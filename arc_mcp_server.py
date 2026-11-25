"""
ARC MCP server: lets an AI assistant create and edit Annotated Research Contexts (ARCs)
using DataPLANT's ARCtrl library.

Every tool loads the ARC from disk, changes it, and writes it back, so the files on disk
are always the single source of truth and you can open them in ARCitISA at any time.

Run:   ARC_ROOT=~/arcs arc-mcp              (installed)
       ARC_ROOT=~/arcs python arc_mcp_server.py   (from a checkout)

Author: Mohamed Abouzid 
Email: m.abouzid@fz-juelich.de
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

import anyio.to_thread
from pydantic import BaseModel, Field

from arctrl import ARC, CompositeCell, CompositeHeader, IOType, OntologyAnnotation, Person

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError


# All ARCs live under this folder. Tools only ever take an ARC *name*, never a full path,
# so the model cannot read or write anywhere else on your disk.
ARC_ROOT = Path(os.environ.get("ARC_ROOT", "~/arcs")).expanduser().resolve()
ARC_ROOT.mkdir(parents=True, exist_ok=True)

mcp = MCPServer(
    "arc",
    instructions=(
        "Tools for building ARCs (Annotated Research Contexts, DataPLANT/nfdi4plants). "
        "An ARC has an investigation, studies (biological material, samples) and assays "
        "(measurements). Typical order: create_arc, add_person, add_study, add_assay, then "
        "add_annotation_table for each protocol step. Use search_ontology to find proper "
        "ontology terms (name, source, accession) before adding parameters, characteristics "
        "or units. Call describe_arc to check the result."
    ),
)


# ---------------------------------------------------------------- helpers

def _arc_dir(name: str) -> Path:
    path = (ARC_ROOT / name).resolve()
    if path.parent != ARC_ROOT:
        raise ValueError(f"Invalid ARC name {name!r}: use a plain folder name.")
    return path


def _load(name: str) -> tuple[ARC, Path]:
    path = _arc_dir(name)
    if not (path / "isa.investigation.xlsx").exists():
        raise ValueError(f"No ARC called {name!r} in {ARC_ROOT}. Use list_arcs or create_arc.")
    return ARC.load(str(path)), path


def _save(arc: ARC, path: Path) -> None:
    # Update (not Write) overwrites the files of an ARC that already exists on disk.
    arc.Update(str(path))


# Tools run in worker threads (see _tool), so parallel tool calls really run in parallel.
# Every ARC tool does load -> modify -> save on the same xlsx files; without this lock,
# concurrent calls read half-written files and can leave them permanently corrupt.
_ARC_LOCK = threading.Lock()


def _tool(fn, *, lock: bool):
    """Register fn as an MCP tool and pass error messages back to the model,
    so it can fix its call (e.g. a typo in a study identifier) instead of giving up.

    The tool body always runs in a worker thread: ARCtrl's load/write call asyncio.run()
    internally, which fails inside the server's event loop (MCP SDK 1.x runs sync tools
    on the loop itself), and blocking I/O should not stall the server anyway."""

    def run(*args, **kwargs):
        if lock:
            with _ARC_LOCK:
                return fn(*args, **kwargs)
        return fn(*args, **kwargs)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await anyio.to_thread.run_sync(functools.partial(run, *args, **kwargs))
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e

    return mcp.tool()(wrapper)


def arc_tool(fn):
    """A tool that reads or writes ARC files (serialised with the other ARC tools)."""
    return _tool(fn, lock=True)


def web_tool(fn):
    """A tool that does not touch ARC files (runs without the ARC lock)."""
    return _tool(fn, lock=False)


# Excel rejects sheet names longer than 31 characters or containing any of these.
_SHEET_FORBIDDEN = set('[]:*?/\\')


def _check_table_name(table_name: str) -> None:
    bad = sorted(_SHEET_FORBIDDEN & set(table_name))
    if not table_name.strip() or len(table_name) > 31 or bad or table_name.startswith("'") or table_name.endswith("'"):
        raise ValueError(
            f"Invalid table name {table_name!r}: it becomes an Excel sheet name, so it must be "
            "1-31 characters, must not contain [ ] : * ? / \\ and must not start or end with '."
        )


class Term(BaseModel):
    """An ontology term. Accession like 'PATO:0000146'; source like 'PATO'."""

    name: str
    source: str = ""
    accession: str = ""


def _oa(t: Term | None) -> OntologyAnnotation | None:
    if t is None:
        return None
    return OntologyAnnotation(t.name, t.source or None, t.accession or None)


class Column(BaseModel):
    """One column of an annotation table (one protocol step)."""

    kind: Literal[
        "input", "output", "parameter", "characteristic", "factor",
        "component", "protocol_ref", "comment",
    ]
    io_type: Literal["source", "sample", "material", "data"] = Field(
        "sample", description="Only for input/output columns."
    )
    term: Term | None = Field(
        None,
        description="Column term for parameter/characteristic/factor/component "
        "(e.g. temperature, PATO, PATO:0000146). For comment, term.name is the comment label.",
    )
    unit: Term | None = Field(
        None, description="If set, values are numbers in this unit (e.g. degree Celsius, UO, UO:0000027)."
    )
    values: list[str | Term] = Field(
        description="One entry per row. Plain strings, or Term objects for ontology-annotated values."
    )


_IO = {"source": IOType.source, "sample": IOType.sample, "material": IOType.material, "data": IOType.data}


def _header(c: Column):
    if c.kind in ("input", "output"):
        io = _IO[c.io_type]()
        return CompositeHeader.input(io) if c.kind == "input" else CompositeHeader.output(io)
    if c.kind == "protocol_ref":
        return CompositeHeader.protocol_ref()
    if c.kind == "comment":
        if not c.term:
            raise ValueError("A comment column needs term.name as its label.")
        return CompositeHeader.comment(c.term.name)
    if not c.term:
        raise ValueError(f"A {c.kind} column needs a term.")
    return getattr(CompositeHeader, c.kind)(_oa(c.term))


def _cell(c: Column, v: str | Term):
    if c.kind in ("input", "output"):
        text = v.name if isinstance(v, Term) else v
        return CompositeCell.create_data_from_string(text) if c.io_type == "data" else CompositeCell.free_text(text)
    if c.kind in ("protocol_ref", "comment"):
        return CompositeCell.free_text(v.name if isinstance(v, Term) else v)
    if c.unit is not None:
        return CompositeCell.create_unitized(v.name if isinstance(v, Term) else v, _oa(c.unit))
    return CompositeCell.create_term(_oa(v) if isinstance(v, Term) else OntologyAnnotation(v))


def _term_str(oa) -> str | None:
    if oa is None or not oa.Name:
        return None
    return f"{oa.Name} ({oa.TermAccessionShort})" if oa.TermAccessionNumber else oa.Name


def _tables(entity) -> list[dict]:
    return [
        {"name": t.Name, "rows": t.RowCount, "columns": [str(h) for h in t.Headers]}
        for t in entity.Tables
    ]


# ---------------------------------------------------------------- tools

@arc_tool
def list_arcs() -> list[str]:
    """List the ARCs in the ARC folder."""
    return sorted(p.name for p in ARC_ROOT.iterdir() if (p / "isa.investigation.xlsx").exists())


@arc_tool
def create_arc(name: str, title: str, description: str = "") -> str:
    """Create a new, empty ARC (folder structure + isa.investigation.xlsx) and git-init it.
    `name` is the folder name and investigation identifier, e.g. 'drought-arabidopsis'."""
    path = _arc_dir(name)
    if path.exists():
        raise ValueError(f"{path} already exists.")
    arc = ARC(name, title=title, description=description or None)
    arc.Write(str(path))
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=path, check=False)
    return f"Created ARC at {path}"


@arc_tool
def describe_arc(name: str) -> str:
    """Summarise an ARC: investigation metadata, people, studies, assays and their tables."""
    arc, path = _load(name)
    summary = {
        "path": str(path),
        "identifier": arc.Identifier,
        "title": arc.Title,
        "description": arc.Description,
        "contacts": [f"{p.FirstName or ''} {p.LastName or ''}".strip() for p in arc.Contacts],
        "studies": [
            {
                "identifier": s.Identifier,
                "title": s.Title,
                "registered_assays": list(s.RegisteredAssayIdentifiers),
                "tables": _tables(s),
            }
            for s in arc.Studies
        ],
        "assays": [
            {
                "identifier": a.Identifier,
                "measurement_type": _term_str(a.MeasurementType),
                "technology_type": _term_str(a.TechnologyType),
                "tables": _tables(a),
            }
            for a in arc.Assays
        ],
    }
    return json.dumps(summary, indent=2, ensure_ascii=False)


@arc_tool
def add_person(
    name: str,
    first_name: str,
    last_name: str,
    email: str = "",
    affiliation: str = "",
    orcid: str = "",
    roles: list[Term] | None = None,
) -> str:
    """Add a contact person to the investigation of ARC `name`."""
    arc, path = _load(name)
    arc.Contacts.append(
        Person(
            orcid=orcid or None,
            last_name=last_name,
            first_name=first_name,
            email=email or None,
            affiliation=affiliation or None,
            roles=[_oa(r) for r in roles] if roles else None,
        )
    )
    _save(arc, path)
    return f"Added {first_name} {last_name} to {name}."


@arc_tool
def add_study(name: str, identifier: str, title: str = "", description: str = "") -> str:
    """Add a study (biological material / sample creation) to ARC `name`."""
    arc, path = _load(name)
    if arc.ContainsStudy(identifier):
        raise ValueError(f"Study {identifier!r} already exists.")
    study = arc.InitStudy(identifier)
    # InitStudy only creates the study; register it so it is listed in isa.investigation.xlsx.
    arc.RegisterStudy(identifier)
    study.Title = title or None
    study.Description = description or None
    _save(arc, path)
    return f"Added study {identifier!r} to {name}."


@arc_tool
def add_assay(
    name: str,
    identifier: str,
    measurement_type: Term | None = None,
    technology_type: Term | None = None,
    technology_platform: Term | None = None,
    study_identifier: str = "",
) -> str:
    """Add an assay (a measurement) to ARC `name`, optionally linked to an existing study."""
    arc, path = _load(name)
    if arc.ContainsAssay(identifier):
        raise ValueError(f"Assay {identifier!r} already exists.")
    assay = arc.InitAssay(identifier)
    assay.MeasurementType = _oa(measurement_type)
    assay.TechnologyType = _oa(technology_type)
    assay.TechnologyPlatform = _oa(technology_platform)
    if study_identifier:
        arc.GetStudy(study_identifier).RegisterAssay(identifier)
    _save(arc, path)
    return f"Added assay {identifier!r} to {name}."


@arc_tool
def add_annotation_table(
    name: str,
    target: Literal["study", "assay"],
    identifier: str,
    table_name: str,
    columns: list[Column],
) -> str:
    """Add an annotation table (one protocol step, like a Swate sheet) to a study or assay.

    Usually starts with an input column and ends with an output column, with parameters,
    characteristics, factors etc. in between. All columns must have the same number of values.
    """
    _check_table_name(table_name)
    lengths = {len(c.values) for c in columns}
    if len(lengths) != 1:
        raise ValueError(f"All columns need the same number of values, got lengths {sorted(lengths)}.")
    if 0 in lengths:
        raise ValueError("Columns need at least one value (one per row).")
    arc, path = _load(name)
    entity = arc.GetStudy(identifier) if target == "study" else arc.GetAssay(identifier)
    if table_name in list(entity.TableNames):
        raise ValueError(f"{target} {identifier!r} already has a table {table_name!r}.")
    table = entity.InitTable(table_name)
    for c in columns:
        table.AddColumn(_header(c), [_cell(c, v) for v in c.values])
    _save(arc, path)
    return f"Added table {table_name!r} with {lengths.pop()} rows to {target} {identifier!r}."


@web_tool
def search_ontology(query: str, ontology: str = "", rows: int = 10) -> list[dict]:
    """Search ontology terms via the EBI Ontology Lookup Service (OLS4).
    Optionally restrict to one ontology prefix, e.g. 'po', 'peco', 'obi', 'uo', 'pato'."""
    params = {"q": query, "rows": rows}
    if ontology:
        params["ontology"] = ontology.lower()
    url = "https://www.ebi.ac.uk/ols4/api/search?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.load(resp)
    def source(d: dict) -> str:
        # Take the source from the accession prefix so they match ('NCBITaxon', not the
        # 'NCBITAXON' that OLS reports as ontology_prefix).
        obo_id = d.get("obo_id") or ""
        if ":" in obo_id:
            return obo_id.split(":", 1)[0]
        return (d.get("ontology_prefix") or d.get("ontology_name", "")).upper()

    return [
        {
            "name": d.get("label"),
            "source": source(d),
            "accession": d.get("obo_id") or d.get("short_form"),
            "iri": d.get("iri"),
            "description": (d.get("description") or [""])[0][:200],
        }
        for d in data.get("response", {}).get("docs", [])
    ]


def main() -> None:
    """Entry point for the `arc-mcp` command."""
    mcp.run()  # stdio transport, which is what Claude Desktop / Claude Code expect


if __name__ == "__main__":
    main()

"""Smoke test: starts the server over stdio, builds a demo ARC and checks the results.
Exits non-zero if anything is wrong. Writes to $ARC_ROOT (default /tmp/arcs-test)."""

import asyncio, json, os, shlex, shutil, sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).resolve().parent / "arc_mcp_server.py"
ARC_ROOT = os.environ.get("ARC_ROOT", "/tmp/arcs-test")
ARC = "drought-arabidopsis"
failures = []


def is_error(res):  # MCP SDK 2.x: is_error, 1.x: isError
    return bool(getattr(res, "is_error", None) or getattr(res, "isError", False))


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


async def main():
    shutil.rmtree(Path(ARC_ROOT) / ARC, ignore_errors=True)
    shutil.rmtree(Path(ARC_ROOT) / "parallel", ignore_errors=True)
    # ARC_MCP_CMD tests an installed server instead, e.g. ARC_MCP_CMD="uvx --from . arc-mcp"
    cmd = shlex.split(os.environ.get("ARC_MCP_CMD", "")) or [sys.executable, str(SERVER)]
    params = StdioServerParameters(command=cmd[0], args=cmd[1:], env={**os.environ, "ARC_ROOT": ARC_ROOT})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("TOOLS:", [t.name for t in (await s.list_tools()).tools])

            async def call(n, a, expect_error=False):
                res = await s.call_tool(n, a)
                text = res.content[0].text if res.content else ""
                check(is_error(res) == expect_error, f"{n} {'fails' if expect_error else 'succeeds'}: {text[:150]}")
                return text

            await call("create_arc", {"name": ARC, "title": "Drought stress in Arabidopsis"})
            await call("add_person", {"name": ARC, "first_name": "Jane", "last_name": "Doe", "affiliation": "FZ Jülich"})
            await call("add_study", {"name": ARC, "identifier": "plant-growth", "title": "Growing Col-0 under drought"})
            await call("add_assay", {"name": ARC, "identifier": "rnaseq", "study_identifier": "plant-growth",
                "measurement_type": {"name": "transcription profiling", "source": "OBI", "accession": "OBI:0000424"}})
            await call("add_annotation_table", {"name": ARC, "target": "study", "identifier": "plant-growth", "table_name": "Growth",
                "columns": [
                    {"kind": "input", "io_type": "source", "values": ["seed batch 1", "seed batch 1"]},
                    {"kind": "characteristic", "term": {"name": "organism", "source": "OBI", "accession": "OBI:0100026"},
                     "values": [{"name": "Arabidopsis thaliana", "source": "NCBITaxon", "accession": "NCBITaxon:3702"}]*2},
                    {"kind": "factor", "term": {"name": "watering exposure", "source": "PECO", "accession": "PECO:0007383"}, "values": ["control", "drought"]},
                    {"kind": "parameter", "term": {"name": "temperature", "source": "PATO", "accession": "PATO:0000146"},
                     "unit": {"name": "degree Celsius", "source": "UO", "accession": "UO:0000027"}, "values": ["22", "22"]},
                    {"kind": "output", "io_type": "sample", "values": ["plant_c1", "plant_d1"]}]})

            # Inputs the model can get wrong must come back as errors, not broken files.
            await call("add_annotation_table", {"name": ARC, "target": "assay", "identifier": "rnaseq", "table_name": "Bad",
                "columns": [{"kind": "input", "values": ["a"]}, {"kind": "output", "values": ["x", "y"]}]}, expect_error=True)
            await call("add_annotation_table", {"name": ARC, "target": "assay", "identifier": "rnaseq", "table_name": "x" * 32,
                "columns": [{"kind": "input", "values": ["a"]}]}, expect_error=True)
            await call("add_annotation_table", {"name": ARC, "target": "assay", "identifier": "rnaseq", "table_name": "Empty",
                "columns": [{"kind": "input", "values": []}]}, expect_error=True)
            await call("add_study", {"name": ARC, "identifier": "../escape"}, expect_error=True)
            await call("create_arc", {"name": "../escape", "title": "x"}, expect_error=True)

            d = json.loads(await call("describe_arc", {"name": ARC}))
            check(d["studies"][0]["registered_assays"] == ["rnaseq"], "assay is registered in its study")
            check(d["studies"][0]["tables"][0]["rows"] == 2, "Growth table has 2 rows")

            # Parallel calls on the same ARC must neither corrupt files nor lose updates.
            await call("create_arc", {"name": "parallel", "title": "P"})
            await call("add_study", {"name": "parallel", "identifier": "st"})
            cols = [{"kind": "input", "values": ["a"]}, {"kind": "output", "values": ["b"]}]
            res = await asyncio.gather(
                *[s.call_tool("add_person", {"name": "parallel", "first_name": f"P{i}", "last_name": "X"}) for i in range(5)],
                *[s.call_tool("add_annotation_table", {"name": "parallel", "target": "study", "identifier": "st",
                                                       "table_name": f"T{i}", "columns": cols}) for i in range(5)])
            check(not any(is_error(x) for x in res), "10 parallel edits all succeed")
            d = json.loads(await call("describe_arc", {"name": "parallel"}))
            check(len(d["contacts"]) == 5, f"5 parallel add_person calls kept (got {len(d['contacts'])})")
            check(len(d["studies"][0]["tables"]) == 5, f"5 parallel tables kept (got {len(d['studies'][0]['tables'])})")


asyncio.run(main())

# Check the files on disk directly with ARCtrl (outside the event loop: ARC.load runs its own).
from arctrl import ARC as Arc
arc = Arc.load(str(Path(ARC_ROOT) / ARC))
check(list(arc.RegisteredStudyIdentifiers) == ["plant-growth"], "study is registered in the investigation")
print(f"\n{len(failures)} failure(s)" if failures else "\nAll checks passed.")
sys.exit(1 if failures else 0)

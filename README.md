![Workflow](images/logo.jpeg)

# ARC MCP server

Lets AI agents (like Claude or any MCP client) create and edit ARCs using DataPLANT's ARCtrl library.
Output is standard ARC files (`isa.*.xlsx`), so you can open and refine them in ARCitISA/Swate.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (it fetches Python 3.12+ itself if needed)
- `git` on your PATH (`create_arc` runs `git init`)

No clone or virtualenv needed: `uvx` downloads the server from GitHub, installs it in an
isolated cached environment and starts it. ARCs are written to `ARC_ROOT` (default `~/arcs`).

## Connect to Claude Code

    claude mcp add arc -e ARC_ROOT=$HOME/arcs -- uvx --from git+https://github.com/nfdi4plants/ARC-MCP arc-mcp

## Connect to Claude Desktop

Settings → Developer → Edit Config, then add:

    {
      "mcpServers": {
        "arc": {
          "command": "uvx",
          "args": ["--from", "git+https://github.com/nfdi4plants/ARC-MCP", "arc-mcp"],
          "env": { "ARC_ROOT": "/absolute/path/to/your/arcs" }
        }
      }
    }

Restart Claude Desktop. If it reports that `uvx` cannot be found (common on macOS), replace
`"uvx"` with the full path printed by `which uvx`.

Any other MCP client works the same way: the server speaks stdio, launched with
`uvx --from git+https://github.com/nfdi4plants/ARC-MCP arc-mcp`.

## Tools

| Tool | Does |
|---|---|
| list_arcs | ARCs in ARC_ROOT |
| create_arc | New ARC skeleton + `git init` |
| describe_arc | Summary of investigation, people, studies, assays, tables |
| add_person | Contact on the investigation |
| add_study / add_assay | New study / assay (assay can be linked to a study) |
| add_annotation_table | A protocol sheet (input, parameters, characteristics, factors, output) |
| search_ontology | Term lookup via EBI OLS4 |

Every tool loads the ARC from disk, edits it, and saves it, so the files are always the source of truth.
Pushing to the DataHUB is left to you: `git add -A && git commit -m "..." && git push`.

### Updating and pinning

`uvx` caches the install. To pull the latest version from GitHub, run
`uvx --refresh --from git+https://github.com/nfdi4plants/ARC-MCP arc-mcp` once (stop it with Ctrl+C),
or pin a tag/commit in the URL: `git+https://github.com/nfdi4plants/ARC-MCP@v0.1.0`.

## Development

    git clone https://github.com/nfdi4plants/ARC-MCP && cd ARC-MCP
    python -m venv .venv
    source .venv/bin/activate          # Windows: .venv\Scripts\activate
    pip install -e .
    python test_client.py              # smoke test, writes a demo ARC to /tmp/arcs-test

`ARC_MCP_CMD` runs the smoke test against an installed server instead of the checkout,
e.g. `ARC_MCP_CMD="uvx --from . arc-mcp" python test_client.py`.

## Author
[Mohamed Abouzid](mailto:m.abouzid@fj-juelich.de)

## Licence
MIT
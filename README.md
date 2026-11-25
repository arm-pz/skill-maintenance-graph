# Skill Maintenance Graph

For maintainers of published agent skills, this workflow turns a health regression into a fix, sends the fix to a fresh verifier, and evaluates the reports with a Python release gate. **Descale** is the name of this workflow in the Skillsmith brew family.

## Try the gate

From a checkout containing the implementation, run the gate tests with Python 3.

```sh
git clone https://github.com/arm-pz/skill-maintenance-graph.git
cd skill-maintenance-graph
python -m unittest discover -s tools/agent-skills/tests -p test_descale_gate.py -v
```

The tests exercise clean, fixed, released and held runs using fixtures. To evaluate a run of your own, follow the [gate CLI guide](tools/descale-graph/README.md).

## Run the full workflow

The conductor is designed to run inside the [Skillsmith catalog checkout](https://github.com/arm-pz/skills-site). Place this repository's `tools/descale-graph/`, `.claude/skills/descale-graph/`, and the three `.claude/agents/descale-*.md` files at their corresponding catalog paths. The catalog supplies the health script and baseline. Configure the companion tools listed in the [graph spec](tools/descale-graph/graph.json) before invoking `/descale-graph` in Claude Code there.

The verifier can send a change back for up to three rounds. A behavior change requires a release report, and the gate checks the final upstream state. For the related workflow that reviews pull requests, see [PR Graph](https://github.com/arm-pz/pr-graph).

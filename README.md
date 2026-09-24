# Hagents: hierarchical agents

A company of agents shaped like a pyramid. Each agent works in its own folder
and keeps its knowledge there. An agent can see the folders of the units below
it, and nothing above or beside it. **System agents** run on the other side of
the pyramid and manage the configuration of the levels below them without seeing
their work. The top level sees and changes everything.

Hagents is built from three projects:
[LoomLoop](https://github.com/ismaelfaro/loomloop) coordinates the agents,
[nanoLoop](https://github.com/ismaelfaro/nanoLoop) is the agent that works in
each folder, and [AgentDorm](https://github.com/ismaelfaro/agentdorm) runs each agent in its own container.

For the design, the invariants and the threat model, see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Quick start (offline, no model)

```bash
git clone https://github.com/ismaelfaro/hagents && cd hagents
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/hagent init ~/acme --runtime mock      # small company, deterministic brains
cd ~/acme
hagent tree                                      # the pyramid
hagent plan engineering                          # its mounts and mail routes
hagent verify                                    # prove the isolation in real containers
hagent ask "Prepare the Q4 launch plan"          # owner -> ceo -> heads -> teams -> back up
hagent inbox                                     # the answer that reached you
hagent rollup                                    # STATUS.md refreshed bottom-up
hagent log                                       # every delivery and refusal
```

## With real agents

nanoLoop (the default runtime):

```bash
docker build -t hagents-nanoloop:local docker/nanoloop
export OPENROUTER_API_KEY=sk-or-...  HARNESS_MODEL=z-ai/glm-5.2:free
hagent init ~/acme                               # runtime = "nanoloop" everywhere
cd ~/acme/work/units/sales && hagent ask "Draft our pricing FAQ"   # targets the folder you are in
```

Hermes, for any unit: set `runtime = "hermes"` in its `node.toml`, then `hagent apply`.
It uses AgentDorm's `hermes-web:local` image (`agentdorm build hermes`):

```bash
export HERMES_PROVIDER=openrouter HERMES_MODEL=poolside/laguna-s-2.1:free
hagent ask finance "What is our runway?"
```

A turn that fails (model error, timeout) keeps its mail; `hagent run` retries it.

## Changing the organisation

System agents (`ops`, `eng-admin`) edit `org/`. So can you. Changes stay
proposals until you apply them:

```bash
hagent ask ops "Hire a data team under engineering"
hagent diff          # review
hagent apply         # validate and make it live: the folder, mounts and routes follow
```

## Tests

```bash
.venv/bin/pytest
```

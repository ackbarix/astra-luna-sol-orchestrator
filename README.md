# Astra–Luna–Sol Orchestrator

Independent, installable Codex/OpenCodex multi-agent orchestration, inspired by [donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator).

**Roles:** GPT-6 Astra Medium (HEAD); GPT-6 Luna Max (parallel explorer, worker, tester, researcher); GPT-6.1 Sol High (complex hard worker); GPT-6.1 Sol Medium (independent reviewer). Maximum six simultaneous children; never spawn needlessly.

## Requirements

Linux/macOS, Python 3.9+, Bash, Git, Codex CLI and configured OpenCodex `ocx`. Project must be trusted by Codex for project configuration to apply.

## Install into an existing development project

```bash
git clone https://github.com/ackbarix/astra-luna-sol-orchestrator.git
cd astra-luna-sol-orchestrator
sh install.sh /data/reactive-shortterm --dry-run
sh install.sh /data/reactive-shortterm
sh install.sh /data/reactive-shortterm --check
```

Or `sh setup.sh` for interactive installation. Replace `/data/reactive-shortterm` with your existing project's path.

## Run

First launch, configure machine-wide OpenCodex once:

```bash
cd /data/reactive-shortterm
bash .astra-luna-sol/run.sh --configure-ocx
```

Next sessions:

```bash
bash .astra-luna-sol/run.sh
```

`bash .astra-luna-sol/run.sh --yolo` explicitly disables Codex approval and sandbox protections. Only use in separately isolated trusted environments. `--dry-run` previews the command.

For complex tasks invoke `$astra-luna-sol` in Codex if the skill is not chosen automatically.

## Verify

```bash
ocx v2 status
ocx agent status
ocx logs --follow
```

Codex `/status` verifies HEAD selection. Actual spawned worker identity and routing require execution/log evidence; role files are not proof of spawn.

## Safe update and removal

```bash
git pull --ff-only
sh install.sh /data/reactive-shortterm --dry-run
sh install.sh /data/reactive-shortterm
sh install.sh /data/reactive-shortterm --uninstall --dry-run
sh install.sh /data/reactive-shortterm --uninstall
```

Installer avoids overwriting existing `.codex/config.toml`, preserves unowned `AGENTS.md` content, records managed-file hashes, and refuses overwriting edited managed paths. `--configure-ocx` changes global OpenCodex settings and project uninstall does not revert those settings.

## Layout

- `install.py`, `install.sh`, `setup.sh`: install/check/uninstall
- `profiles/agents/als_*.toml`: named worker roles
- `profiles/skills/astra-luna-sol/SKILL.md`: orchestration rules
- `profiles/AGENTS.snippet.md`: project instruction block
- `scripts/run.sh`: launcher; `scripts/check-runtime.sh`: status checks

MIT License. Not affiliated with OpenAI, OpenCodex, or the inspiration repository.

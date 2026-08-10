# SaltStack AI Bridge

Generic AI-powered SaltStack and Zabbix integration for Odoo.

## Purpose

This module provides the foundation for AI-driven infrastructure operations.
It contains **zero** infrastructure-specific knowledge — safe to open-source.

## Features

- **SaltAPI** — REST client for SaltStack API (port 8377, POST till ROOT `/` — `/run` ger 401)
- **ZabbixAPI** — JSON-RPC client for Zabbix API
- **Settings** — Configurable API URLs, tokens, and auth methods
- **26 Tools** — 18 SaltStack + 8 Zabbix tools for AI agents
- **6 Skills** — Educational SaltStack and Zabbix knowledge
- **Access Control** — all tools bound to the `Infrastructure Operator` group (`group_infra_operator`)
- **Capabilities** — salt/zabbix tool serialization units via `ai.tool.capability`
- **Extensible** — Bridge modules add infrastructure-specific tools

## Access Control

All salt/zabbix tools are bound to the **Infrastructure Operator** group
(`saltstack_ai.group_infra_operator`). Users without this group cannot see
or use the tools — even read-only tools (pillar/grains contain secrets).

Tools are also organized into `ai.tool.capability` records (`cap_salt_ops`,
`cap_zabbix_ops`) that define how the AI serializes tool usage. Access
filtering happens **before** serialization, so unauthorized members are
hidden both as tools and as operations.

## Installation

```bash
# Install dependencies
sudo checkmodule -d <db> -m saltstack
sudo checkmodule -d <db> -m ai_agent_core

# Install this module
sudo checkmodule -d <db> -m saltstack_ai
```

## Configuration

1. Go to **Settings > General Settings**
2. Scroll to **SaltStack & Zabbix**
3. Configure:
   - Salt API URL + Token (or select Keykeep Managed)
   - Zabbix API URL + Token (or select Keykeep Managed)

## Tools

### SaltStack Tools
| Tool | Risk | Description |
|------|------|-------------|
| salt_test_ping | safe | Ping minions |
| salt_state_apply | destructive | Apply Salt state |
| salt_state_highstate | destructive | Run highstate |
| salt_state_show_sls | read_only | Preview compiled state |
| salt_cmd_run | destructive | Execute shell command |
| salt_pillar_get | read_only | Get pillar value |
| salt_pillar_items | read_only | Get all pillar data |
| salt_grains_get | read_only | Get specific grain |
| salt_grains_items | read_only | Get all grains |
| salt_minion_list | read_only | List all minions |
| salt_minion_accept | destructive | Accept minion key |
| salt_service_status | read_only | Check service status |
| salt_service_restart | destructive | Restart service |
| salt_disk_usage | read_only | Check disk space |
| salt_memory_usage | read_only | Check memory |
| salt_system_load | read_only | Check CPU load |
| salt_journal_errors | read_only | Get journal errors |
| salt_process_list | read_only | List processes |

### Zabbix Tools
| Tool | Risk | Description |
|------|------|-------------|
| zabbix_get_alerts | read_only | Get active problems |
| zabbix_get_host | read_only | Get host details |
| zabbix_get_problems | read_only | Get all problems |
| zabbix_acknowledge | write | Acknowledge event |
| zabbix_get_history | read_only | Get metric history |
| zabbix_get_item | read_only | Get item definition |
| zabbix_get_host_groups | read_only | Get host groups |
| zabbix_get_triggers | read_only | Get triggers |

## Usage with AI Coworkers

```python
# Tools are ai.tool records — assign to any ai.coworker:
coworker.write({'tool_ids': [(6, 0, [
    ref('saltstack_ai.tool_salt_test_ping'),
    ref('saltstack_ai.tool_zabbix_get_alerts'),
    # ... etc
])]})
```

## Dependencies

- `saltstack` — Base filesystem bridge
- `ai_agent_core` — AI agent engine
- `mail` — Chatter and notifications

## License

AGPL-3 — Copyright (C) 2026 Vertel Sverige AB

# Employment Resume Search

A Codex skill for translating Chinese resume criteria into parameterized, read-only ClickHouse searches.

This repository is private because the skill bundles access to a recruitment database. Grant repository access only to authorized users.

## Install

Ask Codex to install the skill from this repository, or run:

```bash
python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo SongLiang666/employment-resume-search \
  --path skills/clickhouse-resume-search
```

The skill becomes available to Codex on the next turn.

Because this is a private repository, each recipient must be added as a GitHub collaborator and authenticate Git/GitHub before installation.

## Connection

The read-only ClickHouse connection is bundled in the installed skill, so recipients do not create or edit a configuration file. On its first query, the executor automatically restricts `config/connection.json` to owner-only permissions (`0600`) on POSIX systems. Recipients must still be connected to the network that can reach the configured ClickHouse server.

## Use

Examples:

- `找最近刷新、会 Java 和支付系统的简历。`
- `找 30 岁以下、3 年以上工作经验的候选人，返回 50 条。`
- `搜索工作经历或项目经历里包含风控的简历。`

未明确指定字段的关键词会按“期望职位名称 -> 当前职位 -> 工作经历 -> 项目经历 -> 简历名称或学校”逐级检索；某一级有结果就停止，不会用低优先级结果补齐。

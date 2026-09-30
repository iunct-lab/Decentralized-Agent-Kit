# スキル: ADK 標準 SkillToolset へ移行するか

PBI #90。DAK は独自の `SkillRegistry`（`agent/dak_agent/skill_registry.py`）と `list_skills` / `enable_skill`
（`agent/dak_agent/skill_tools.py`）でスキルを扱っている。google-adk の標準 `SkillToolset` に寄せて独自の保守を減らせるかを、
機能差分（#216）と既存 3 スキル（`filesystem` / `solana_wallet` / `dependency_maintenance`）の挙動から判断する。

結論: **今は移行しない（DAK 独自を維持）**。理由と、見直す条件は「判断」節。

## 比べたもの（2026-09-30）

- ADK: google-adk 2.8.0（`agent/uv.lock`）の `google/adk/tools/skill_toolset.py`（`SkillToolset`、`ListSkillsTool`、`LoadSkillTool`、
  `LoadSkillResourceTool`、`RunSkillScriptTool`、`SearchSkillsTool`）、`google/adk/skills/`（`models.py`、`skill_registry.py`、`_utils.py`）、
  `google/adk/features/_feature_registry.py`
- DAK: `skill_registry.py`、`skill_tools.py`、`adaptive_agent.py`（`_resolve_session_instruction` / `_resolve_session_tools`）、
  `agent.py`、`permission.py`、`agent/skills/*/SKILL.md`
- 実物で試したこと: ADK の `load_skill_from_dir` と `_validate_skill_dir` に 3 スキルを読ませた
  - `filesystem`: 読める。`tools` は `Frontmatter.model_extra` に残るだけで、どこからも使われない
  - `solana_wallet` / `dependency_maintenance`: `ValidationError`（`name must be lowercase kebab-case`）。
    実験的フラグ `ADK_ENABLE_SNAKE_CASE_SKILL_NAME=1`（既定 off）なら読める
  - `_validate_skill_dir` は 3 つとも `Unknown frontmatter fields: ['tools']`

## 差分

| 観点 | DAK 独自（SkillRegistry + list_skills / enable_skill） | ADK 標準（SkillToolset） |
|---|---|---|
| 段階的開示 | 2 段。`list_skills`（名前と説明。既定の MCP サーバの個別ツールも一覧に混ぜる）→ `enable_skill`（本文をシステム指示に足し、ツールを載せる）。有効なスキルはセッション状態 `dak_active_skills`。`dak:tools` の指定がある呼び出しでは `enable_skill` を断る。モード切替（Meta-LLM）もスキルを選ぶ | 3 段と検索。`list_skills`（XML）→ `load_skill`（本文をツールの応答として返す）→ `load_skill_resource` / `run_skill_script`。`search_skills` はリモートの registry を渡したときだけ。有効なスキルは状態 `_adk_activated_skill_<エージェント名>`。MCP の個別ツールを有効にする経路は無い |
| リソース | 無い（SKILL.md の本文だけ。`tools.py` はツールの実装で、LLM が読むリソースではない） | `references/` / `assets/` / `scripts/` を読み込み、`load_skill_resource` で返す。スクリプトは `code_executor` か `environment` で実行する（どちらも無ければ `NO_CODE_EXECUTOR`） |
| ツールの紐づけと MCP 接続 | frontmatter の `tools`（DAK 独自のキー）。有効にしたとき `tools.py` の同名の関数を FunctionTool にし、無い名前を MCP から取る。`mcp_server`（`agent_config.yaml` の `mcp_servers` の名前）で接続先を選べる。`(URL, 型, 名前の集合)` ごとにフィルタ済みの `McpToolset` をキャッシュする | frontmatter の `metadata.adk_additional_tools`（名前のリスト）。候補は `SkillToolset(additional_tools=[...])` に構築時に渡した BaseTool / BaseToolset から名前で拾う。スキルごとの接続先の指定は無い |
| AP2 連携 | `ENABLE_AP2_PROTOCOL` なら root にウォレットの 4 ツール。セッションで `solana_wallet` 以外のスキルを有効にすると、ウォレットの 3 ツールを自動で足す（`_resolve_session_tools`） | 相当する仕組みは無い |
| 名前 | frontmatter の `name`（無ければディレクトリ名）。snake_case を使っている | kebab-case で、ディレクトリ名と一致すること。snake_case は実験的フラグ（既定 off） |
| ツール名 | `list_skills` / `enable_skill` | `list_skills` が DAK と同名（`tool_name_prefix` で避けられる） |
| 権限 | スキルの FunctionTool は `local` なので既定で allow（`permission.py` の `DEFAULT_RULES`） | `run_skill_script` もエージェントの中で動くツールなので `local` → allow になる。スクリプトを確認なしで実行する |
| コンテキスト | 有効なスキルの本文は毎ターンのシステム指示に入る | 本文はツールの応答なので、ハーネスの古いツール結果の剪定（`prune_old_tool_results`）と圧縮の対象になる |

補足: `SkillRegistry.validate_skills_against_tools` はどこからも呼ばれていない（このPBIでは消さない）。

## 移行したときの 3 スキルの扱い

### filesystem

ツール（`list_files` / `read_file` / `write_file` / `search_files`）は既定の MCP サーバにある。今は `enable_skill` で
`make_mcp_toolset` のフィルタ済みの toolset として載る。

ADK では、既定の MCP サーバの `McpToolset` を `additional_tools` に渡し、SKILL.md に
`metadata: {adk_additional_tools: [list_files, read_file, write_file, search_files]}` と書けば、`load_skill` の後に同じ 4 ツールが載る。
**同じ挙動を保てる**（名前は kebab-case のままで通る）。違いは、本文がシステム指示ではなくツールの応答に入ることと、
`list_skills` の一覧に MCP の個別ツールが出なくなること（DAK 側で別のツールとして残す必要がある）。

### solana_wallet

ツールは `agent/skills/solana_wallet/tools.py` のローカル関数で、`load_local_tools_from_skill` が FunctionTool にしている。
ADK の標準の形はスクリプトの実行（`scripts/` を `code_executor` で走らせ、標準出力を返す）で、関数の呼び出しではない。
関数のまま使うなら、4 関数を FunctionTool にして `additional_tools` に渡し、`adk_additional_tools` で名前を並べることになる。

- ツールは載せられる。ただし `tools.py` を読み込む処理は DAK に残る（`SkillToolset` は `tools.py` を読まない）
- 名前 `solana_wallet` は kebab-case に改名（`solana-wallet`）するか、実験的フラグを使う必要がある。
  改名すると、モード切替の `selected_skills` やセッション状態に残った `solana_wallet` と食い違う
- AP2 のときの「他のスキルを有効にしたらウォレットの 3 ツールを足す」は ADK に無いので、DAK 側で作り直す
- **そのままでは同じ挙動を保てない**

### dependency_maintenance

`solana_wallet` と同じくローカル関数（`classify_bump` / `triage_dependency`）。名前も snake_case。
関数を FunctionTool にして渡せばツールは載るが、改名かフラグが要り、`tools.py` の読み込みは DAK に残る。
**そのままでは同じ挙動を保てない**（名前の点）。

## 判断

**移行しない。DAK 独自の `SkillRegistry` と `list_skills` / `enable_skill` を維持する。部分移行もしない。**

根拠:

1. ADK 標準の強み（`references/` / `assets/` / `scripts/` とスクリプトの実行、リモート registry の検索）を使うスキルが今は無い。
   3 スキルとも `scripts/` などを持たず、ツールはローカル関数か MCP ツール
2. 移行しても独自の保守は減らない。`tools.py` の読み込み、`mcp_server` による接続先の選択、AP2 のウォレットの自動追加、
   MCP の個別ツールの有効化、`dak:tools` とモード切替との連動、`list_skills` の名前の衝突は ADK に無く、DAK 側に残るか作り直しになる
3. 3 スキルのうち 2 つは名前が ADK の規則（kebab-case）に合わない。改名はセッション状態とモード切替の選択に波及し、
   フラグは実験的（既定 off）
4. `run_skill_script` を足すと、エージェントの中でスクリプトを確認なしに実行する経路が増える（権限のルールを足さない限り `local` → allow）

部分移行（例: `filesystem` だけを `SkillToolset` に載せる）も採らない。スキルの有効化の経路が 2 つになり、
`list_skills` の一覧・セッション状態・モード切替がそれぞれ両方を扱う必要が出て、保守は増える。

移行を見直す条件（どれかが起きたら、この文書を更新して判断し直す）:

- `references/` や `scripts/` を持つスキルを足したくなった
- ADK が snake_case の名前を既定で受け付けるようになった、またはスキルのツールをセッションごとに動的に選ぶ仕組みを持った
- 外部の Agent Skills（agentskills.io 形式）のスキルをそのまま読み込みたくなった

移行すると決めたときは、実装を別の Issue に切り出す（このPBIのスコープ外）。

## 未検証のこと

- ADK の `code_executor` / `environment` によるスクリプト実行を、DAK の構成（コンテナ、`PermissionPlugin`、ハーネス）で動かしてはいない
- `load_skill` の応答（スキルの本文）がハーネスの剪定・圧縮で消えたとき、モデルがスキルの手順を守り続けるかは確かめていない
- AP2 のウォレットの自動追加を `SkillToolset` の上でどう表すか（`additional_tools` と `adk_additional_tools` で足りるか、
  DAK 独自の処理が要るか）は、コードで試していない
- 実際の LLM で、2 段（DAK）と 3 段（ADK）の開示のどちらがスキルを正しく使えるかは比べていない

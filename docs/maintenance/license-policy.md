# 依存のライセンスの方針

DAK 自体は Apache-2.0（`LICENSE`）。この文書は、5 つのコンポーネント（`agent`, `mcp-server`, `bff`, `cli`, `maintenance`）の
**実行時の依存**（dev を除く）のライセンスを、どう取り、どう判定し、何を許容するかを書く（PBI #344）。

- 対象外: コンテナイメージの OS パッケージ、`tests/integration/` の依存、各コンポーネントの dev の依存、`bff` が CDN から読む htmx
- ライセンスの法的な解釈（弱いコピーレフトを認めるかなど）は利用者が決める。仕組みは決めた方針を機械的に当てはめるだけ

> **状態（2026-10-07）**: 方針は利用者が決めた（下の「決定」。PBI #344 の決定ログに回答の原文）。
> `maintenance/license-policy.toml`（#346）はこの決定を写したもの。

## 取り方

各コンポーネントのディレクトリで:

```bash
uv sync --frozen --no-dev
uvx pip-licenses==5.5.5 --python .venv/bin/python --with-system --from=all --format=json --with-urls
```

決めたこと:

- **`pip-licenses` を `uvx` で別に動かし、`--python` でコンポーネントの環境を読む**。`uv run --with` だと `pip-licenses` 自身とその依存が一覧に混ざる
- **`--with-system` を付ける**。付けないと `pip-licenses` は自分の依存と同じ名前のパッケージ（`wcwidth`, `prettytable` など）を一覧から落とす。`cli` の実行時の依存の `wcwidth` が実際に落ちた
- **`--from=all` を使い、どの欄を採るかは判定の側（`dak-maint license-check`）で決める**。`--from=mixed` は分類子（`License :: OSI Approved :: BSD License`）があるとメタデータの `License` より分類子を採るので、`Authlib` は `BSD-3-Clause` と書いてあるのに `BSD License` になる。`--from=all` なら 3 つの欄（`License-Expression`, `License-Metadata`, `License-Classifier`）がそろう。採る順は `License-Expression`（PEP 639）→ `License-Metadata`（SPDX にそろえられるとき）→ `License-Classifier`
- `--from=mixed` も `License-Expression` を拾う（両方を取って突き合わせ、`License-Expression` があるパッケージで食い違いは 0 件だった）

棚卸しは 2026-10-01、Linux（aarch64）で取った。Python は `uv sync` が選んだ版で、`agent` が 3.12（`requires-python` が `<3.14`。Dockerfile も 3.12）、`mcp-server` が 3.10（`.python-version`。3.10 でだけ入る `exceptiongroup` も一覧にある）、`bff` が 3.14（`.python-version`）、`cli` と `maintenance` が 3.14。

### 件数の突き合わせ

`uv export --frozen --no-dev --format requirements-txt` の `==` の行と比べた（コンポーネント自身を除く）。差はすべて環境マーカーで入らないもの:

| コンポーネント | `pip-licenses` | `uv export` の `==` 行 | 差の内訳 |
|---|---|---|---|
| agent | 130 | 133 | `colorama`, `pywin32`, `tzdata`（`sys_platform == 'win32'`） |
| mcp-server | 31 | 33 | `colorama`, `pywin32`（win32） |
| bff | 19 | 20 | `colorama`（win32） |
| cli | 15 | 18 | `colorama`（win32）。`click` と `markdown-it-py` は Python の版ごとに 2 行ずつ |
| maintenance | 13 | 14 | `typing-extensions`（`python_full_version < '3.13'`） |

`pip-licenses` はインストール済みの環境を読むので、Linux で取ると Windows でだけ入る依存は見えない。`cli` は利用者の端末にも入るので、この 3 件は PyPI と配布物で手で確かめた（どれも許容的）:

| パッケージ（版） | コンポーネント | 欄の値 | SPDX |
|---|---|---|---|
| `colorama` 0.4.6 | agent, mcp-server, bff, cli | 分類子 `BSD License` | `BSD-3-Clause`（LICENSE.txt） |
| `pywin32` 311 | agent, mcp-server | メタデータ `PSF` | `PSF-2.0 AND BSD-3-Clause`（`win32/`・`Pythonwin/`・`com/` の License.txt は BSD 3 条項の文） |
| `tzdata` 2025.2 | agent | メタデータ `Apache-2.0` | `Apache-2.0` |

Python の版でだけ入る依存も同じく環境に見えない。そのうち `click` 8.1.8（cli、`python_full_version < '3.10'`）は PyPI の欄が分類子 `BSD License` だけで、wheel の `LICENSE.txt` を読んで `BSD-3-Clause` と確かめた（2026-10-07）。

CI（Linux）の環境の一覧だけではこの 3 件が見えないので、`uv export --frozen --no-dev` の一覧と環境の一覧を突き合わせ、環境に無い依存は PyPI の JSON（`https://pypi.org/pypi/<名前>/<版>/json`）のライセンス欄で判定する（下の「決定」。#346/#347）。

## 道具の比較

| 道具 | 何を読むか | uv | 式（`AND` / `OR`） | 判定の柔軟さ | 採否 |
|---|---|---|---|---|---|
| `pip-licenses` 5.5.5（MIT） | インストール済みの環境のメタデータ（`--python` で別の環境） | `uv sync` した `.venv` を読める | 5.5.0 から `License-Expression` を読む。判定（`--allow-only` / `--fail-on`）は文字列の部分一致で、式を解かない | パッケージごとの例外と理由を持てない（`--ignore-packages` は理由を残さない） | **一覧の取得に使う** |
| `licensecheck` 2026.0.8（MIT） | `pyproject.toml` か requirements を読み、自分で PyPI に問い合わせて依存を解く | `uv.lock` を読まない（`uv export` を渡すことはできる） | 自分のライセンスとの両立を判定する | `--ignore-packages` は理由を残さない。lock と違う版を解きうる | 使わない |
| `actions/dependency-review-action` | GitHub の dependency graph | 対応一覧に uv（`uv.lock`）が無い（2026-10-01 に確認: [Dependency graph supported package ecosystems](https://docs.github.com/en/code-security/supply-chain-security/understanding-your-software-supply-chain/dependency-graph-supported-package-ecosystems)。Python は pip と Poetry だけ） | `allow-licenses` | — | 使えない |
| `reuse` 6.2.0 | 自分のファイルのライセンス表記（REUSE） | — | — | 依存は見ない | 対象外 |

判定は `maintenance/` の `dak-maint license-check`（#346）に置く。表記ゆれの正規化、`AND` / `OR` の式、パッケージごとの例外と理由を扱い、止まった理由を分かる言葉で出すため。

## 棚卸し

### コンポーネント × 表記（`--from=mixed` の `License`）

| 表記 | agent | mcp-server | bff | cli | maintenance |
|---|---|---|---|---|---|
| `MIT` | 26 | 12 | 6 | 2 | 2 |
| `MIT License` | 21 | 5 | 2 | 5 | 3 |
| `BSD-3-Clause` | 11 | 7 | 6 | 2 | 2 |
| `Apache Software License` | 23 |  |  | 1 | 1 |
| `Apache-2.0` | 20 | 1 | 1 |  | 2 |
| `BSD License` | 11 | 2 | 2 | 2 | 1 |
| `Mozilla Public License 2.0 (MPL 2.0)` | 1 | 1 | 1 | 1 | 1 |
| `PSF-2.0` | 1 | 1 | 1 | 1 |  |
| `Apache Software License; BSD License` | 2 |  |  |  | 1 |
| `Apache-2.0 OR BSD-3-Clause` | 1 | 1 |  |  |  |
| `ISC License (ISCL)` | 1 |  |  | 1 |  |
| `Apache Software License; MIT License` | 1 | 1 |  |  |  |
| `Python Software Foundation License` | 1 |  |  |  |  |
| `Apache-2.0 AND MIT` | 1 |  |  |  |  |
| `Unlicense` | 1 |  |  |  |  |
| `MIT AND Python-2.0` | 1 |  |  |  |  |
| `Apache License 2.0` | 1 |  |  |  |  |
| `3-Clause BSD License` | 1 |  |  |  |  |
| `Apache-2.0 AND CNRI-Python` | 1 |  |  |  |  |
| `GNU Library or Lesser General Public License (LGPL)` | 1 |  |  |  |  |
| `MIT License; Mozilla Public License 2.0 (MPL 2.0)` | 1 |  |  |  |  |
| `MIT License` + ライセンス本文 | 1 |  |  |  |  |
| `UNKNOWN` | 1 |  |  |  |  |
| 計 | 130 | 31 | 19 | 15 | 13 |

`; ` は `pip-licenses` が複数の分類子をつないだもので、`AND` か `OR` かは分からない（`packaging` は OR、`tqdm` は自分のメタデータに `MPL-2.0 AND MIT`）。

### SPDX の ID への対応の案

| 表記（どの欄にも出るもの） | SPDX |
|---|---|
| `MIT License`, `The MIT License (MIT)` | `MIT` |
| `Apache Software License`, `Apache 2.0`, `Apache License 2.0`, `Apache License, Version 2.0` | `Apache-2.0` |
| `3-Clause BSD License` | `BSD-3-Clause` |
| `ISC License (ISCL)`, `ISC License` | `ISC` |
| `Python Software Foundation License` | `PSF-2.0` |
| `Mozilla Public License 2.0 (MPL 2.0)` | `MPL-2.0` |
| `The Unlicense (Unlicense)` | `Unlicense` |
| 分類子の `; ` | `AND` とみなす（緩い方に倒さない） |

表記だけでは決められないもの（`BSD License`, `BSD`, `Dual License`, `LGPL with exceptions`, ライセンス本文, 空）は、3 つの欄の採る順で `License-Expression` → `License-Metadata` を見たあとでも決まらなければ「不明」にする。棚卸しでは次の 11 件がそうなった。配布物の LICENSE ファイル（または配布元のリポジトリ）を読んで確かめた結果:

| パッケージ（版） | コンポーネント | 欄の値 | LICENSE ファイルで確かめた SPDX |
|---|---|---|---|
| `fastuuid` 0.14.0 | agent | 分類子 `BSD License` | `BSD-3-Clause` |
| `Jinja2` 3.1.6 | agent, bff | 分類子 `BSD License` | `BSD-3-Clause` |
| `prompt_toolkit` 3.0.52 | cli | 分類子 `BSD License` | `BSD-3-Clause` |
| `sqlparse` 0.5.3 | agent | 分類子 `BSD License` | `BSD-3-Clause` |
| `wrapt` 1.17.3 | agent | メタデータ `BSD` | `BSD-2-Clause` |
| `pyasn1_modules` 0.4.2 | agent | メタデータ `BSD` | `BSD-2-Clause` |
| `packaging` 25.0 | agent | 分類子 `Apache Software License; BSD License` | `Apache-2.0 OR BSD-2-Clause`（`LICENSE` が LICENSE.APACHE か LICENSE.BSD のどちらかと書く） |
| `python-dateutil` 2.9.0.post0 | agent, maintenance | メタデータ `Dual License` | `Apache-2.0 AND BSD-3-Clause`（2017-12 以降の寄与は両方、それ以前は BSD-3-Clause） |
| `tiktoken` 0.12.0 | agent | メタデータにライセンス本文 | `MIT` |
| `jsonalias` 0.1.1 | agent | どの欄も空 | `MIT`（配布物に LICENSE が無い。配布元 [kevinheavey/jsonalias](https://github.com/kevinheavey/jsonalias/blob/master/LICENSE) が MIT） |
| `psycopg2-binary` 2.9.13 | agent | 分類子 `GNU Library or Lesser General Public License (LGPL)`、メタデータ `LGPL with exceptions` | `LGPL-3.0-or-later`（LICENSE が LGPL 3 以降と、OpenSSL との結合を認める例外を書く） |

### 判断が要る依存

| パッケージ | ライセンス | コンポーネント | なぜ入っているか（`uv tree --invert`） | 論点 |
|---|---|---|---|---|
| `certifi` | `MPL-2.0` | 5 つすべて | 推移的: `httpx` → `httpcore` → `certifi`（ほか `requests`） | 弱いコピーレフト（ファイル単位）。DAK は改変しない。HTTP を使うどのコンポーネントにも入るので、外すのは現実的でない |
| `tqdm` | `MPL-2.0 AND MIT` | agent | 推移的: `openai` → `tqdm`、`litellm` → `tokenizers` → `huggingface-hub` → `tqdm` | 同上 |
| `psycopg2-binary` | `LGPL-3.0-or-later`（OpenSSL の例外つき） | agent | **直接**（`agent/pyproject.toml`）。`SESSION_SERVICE_URI` を `postgresql://` で渡したときの SQLAlchemy の既定のドライバ。compose の既定は `postgresql+asyncpg://` | 弱いコピーレフト。wheel は共有ライブラリを同梱する（下の表） |
| `jsonalias` | メタデータ無し（配布元は `MIT`） | agent | 推移的: `solders` → `jsonalias`、`solana` → `solders` | 「不明」で止まる。配布元のライセンスを確かめた上で個別に認めるか |
| `regex` | `Apache-2.0 AND CNRI-Python` | agent | 推移的（`tiktoken`） | 許容的だが一覧に入れるか |
| `greenlet` | `MIT AND Python-2.0` | agent | 推移的（`sqlalchemy`） | 同上 |
| `aiohappyeyeballs`, `typing_extensions` | `PSF-2.0` | agent ほか | 推移的（`aiohttp` ほか） | 同上 |
| `filelock` | `Unlicense` | agent | 推移的（`huggingface-hub`） | 同上 |
| `shellingham` | `ISC` | agent, cli | 推移的（agent は `huggingface-hub`、cli は `typer`） | 同上 |

#### `psycopg2-binary` 2.9.13 が同梱する共有ライブラリ

`psycopg2_binary.libs/` にある。wheel の SBOM（`sboms/auditwheel.cdx.json`）は AlmaLinux 8 の RPM から入れたものだけを名前と版で挙げ、ライセンスは書かない。ライセンスは AlmaLinux 8 の spec（`git.almalinux.org/rpms/<名前>` の `c8` 枝）の `License:`、SBOM に無いものは各プロジェクトのライセンス:

| ライブラリ | 出どころ（版） | ライセンス |
|---|---|---|
| `libpq` | SBOM に無い（`libpq.so.5.17`、PostgreSQL 17 系） | PostgreSQL License |
| `libssl` / `libcrypto`（3 系） | SBOM に無い（`.so.3`） | Apache-2.0（OpenSSL 3.0 以降） |
| `libldap` / `liblber` | SBOM に無い（OpenLDAP） | OLDAP-2.8 |
| `libcrypto`（1.1.1k） | `openssl-libs` 1.1.1k-17.el8_6 | `OpenSSL and ASL 2.0` |
| `libkrb5` ほか krb5 | `krb5-libs` 1.18.2-34.el8_10 | `MIT` |
| `libcom_err` | `libcom_err` 1.45.6-7.el8_10 | `MIT` |
| `libselinux` | `libselinux` 2.9-11.el8_10 | `Public Domain` |
| `libkeyutils` | `keyutils-libs` 1.5.10-9.el8 | `GPLv2+ and LGPLv2+`（spec のパッケージ全体の表記） |
| `libcrypt` | `libxcrypt` 4.1.1-6.el8 | `LGPLv2+ and BSD and Public Domain` |
| `libpcre2-8` | `pcre2` 10.32-3.el8_6 | `BSD` |
| `libsasl2` | `cyrus-sasl-lib` 2.1.27-6.el8_5 | `BSD with advertising` |

LGPL の部分（psycopg2 本体、`libkeyutils`、`libcrypt`）が論点になる。DAK はこの wheel を改変せず、コンテナイメージも公開していない（`.github/workflows/` にイメージを push するジョブは無い）。

## 方針

### 案（2026-10-01 に利用者に尋ねたもの）

許容の一覧（`allow`）:

- 許容的: `MIT`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`, `PSF-2.0`, `Python-2.0`, `CNRI-Python`, `Unlicense`, `0BSD`
- 弱いコピーレフト（ファイル単位）: `MPL-2.0` — `certifi` が 5 つすべてに入るため

個別に認める依存（`[[exceptions]]`。パッケージ・ライセンス・理由・確認日）:

- `psycopg2-binary`（agent のみ）: `LGPL-3.0-or-later` — 改変せず、動的に読み込むライブラリとして使う。LGPL を一覧には入れず、このパッケージだけ認める
- `jsonalias`（agent のみ）: `MIT` — 配布物にメタデータが無いが、配布元のリポジトリの LICENSE が MIT
- 上の表の「表記だけでは決められない」残りの 9 件（`fastuuid` ほか）: LICENSE ファイルで確かめた SPDX を記録する

許容外か不明の依存が入ったら CI の license ジョブが止まり、例外を足すかどうかを利用者が決める。

### 決定

2026-10-07、利用者の回答（PBI #344 の決定ログ）:

- **許容の一覧は案のとおり**: 許容的なもの（`MIT`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`, `PSF-2.0`, `Python-2.0`, `CNRI-Python`, `Unlicense`, `0BSD`）と `MPL-2.0`。LGPL は一覧に入れない
- **個別に認めるのは `psycopg2-binary`（agent、`LGPL-3.0-or-later`）と `jsonalias`（agent、`MIT`）だけ**。表記だけでは決められない残りの 9 件は、確かめた SPDX の記録（許容の一覧の中のライセンスに読み替えるだけで、例外ではない）
- **Windows でだけ入る依存も CI で見る**: `uv export --frozen --no-dev` の一覧と環境の一覧を突き合わせ、環境に無い依存は PyPI の JSON のライセンス欄で判定する。PyPI の表記も同じ正規化と方針で判定し、決まらなければ「不明」で止める

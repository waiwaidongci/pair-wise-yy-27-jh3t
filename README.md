# 数字人文文本校勘 · 封存与授权移交闭环

Python 标准库实现的校勘工作台：SQLite 保存作品、版本、残片、转录、段落、异文、注释、修订层、封存快照、转交记录，`http.server` 暴露 JSON API，单页界面即可完成封存、转交、确认与历史查阅。

## 启动与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8114`，地址 <http://127.0.0.1:8114>。首次启动创建示例作品，含负责人（#1）、接任负责人（#2，owner 身份）、校勘编辑（#3）。数据库可用 `COLLATION_DB` 指定，端口可用 `PORT` 指定。

## 业务规则

- 版本类型限定为 `version`、`fragment`、`transcription`；`[缺页]`、`[不可辨]`、`[残损]` 等标记括号必须成对。
- 只有现任负责人或被单独授权的编辑可以写对应版本；其他人只有查看权限。
- 每次新增或修改异文产生递增修订号和 JSON 快照；提交必须携带 `expected_revision`，旧页面不能覆盖新层。

### 封存闭环

- **封存（`POST /api/passages/{id}/seal`，旧 `lock` 路径保留为别名）**：仅现任负责人可执行；封存时留存异文、注释、对齐快照与当时的缺口依据；存在未确认修订时拒绝封存。
- **封存后只补不改**：对齐、已交付异文均不可改动，重复封存被拒绝；仍可补录注释、新增异文，补录内容带 `sealed_supplement` 标记，单独放在导出的 `supplements` 中，不进入交付稿。封存后补到已交付异文上的注释也只进入补录区。
- **修订确认（`POST /api/variants/{id}/confirm`）**：只有负责人可确认；`pending → confirmed` 只生效一次，并发提交在写事务内复查，仅第一次成功。
- **交付稿**：未确认修订一律不进入 `variants`；未封存段落的已确认异文进入交付稿；已封存段落的交付内容固定为封存快照。

### 负责人转交

- `POST /api/works/{id}/handover`：仅现任负责人可转交；接手人必须是 `owner` 角色，否则拒绝（转交给自己同样拒绝）。
- 转交时登记未处理修订数与未封存段落数（`handover_log`），封存责任一并移交。
- 转交后原负责人立即失去该作品写权限（作品负责人身份转移、版本编辑授权收回），仅保留查看权限。
- 转交与确认的并发提交由 `BEGIN IMMEDIATE` 事务串行裁决，只成功一次。

### 封存依据（缺口标记）

- 作品级缺口依据默认 `["[缺页]","[残损]"]`，可用 `POST /api/works/{id}/basis` 修改（仅负责人）。
- 修改依据后该作品**全部旧封存立即失效**：段落回到未封存，并写入 `invalidated` 事件；缺口统计和交付稿导出始终按当前依据实时重算。封存事件历史及封存快照永久保留可查。

## 主要接口

- `POST /api/users`、`POST /api/works`
- `POST /api/works/{id}/witnesses`、`POST /api/witnesses/{id}/editors`
- `POST /api/works/{id}/passages`、`POST /api/works/{id}/access`
- `POST /api/alignments`
- `POST /api/variants`、`POST /api/variants/{id}/revisions`、`POST /api/variants/{id}/confirm`
- `POST /api/notes`
- `POST /api/passages/{id}/seal`（`/lock` 为兼容别名）
- `POST /api/works/{id}/handover`、`POST /api/works/{id}/basis`
- `GET /api/passages/{id}/snapshots/{revision}?user_id=...`
- `GET /api/works/{id}/collation?user_id=...`
- `GET /api/works/{id}/history?user_id=...`（转交记录、封存/失效事件、修订层、确认记录）
- `GET /api/state`（页面总览：作品、段落、异文状态、当前封存、转交记录）

导出接口把版本对齐、异文、注释、残损缺口、封存信息和补录内容组合成可复核的校勘稿。

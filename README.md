# 数字人文文本校勘

这是一个 Python 标准库实现的校勘工作台，使用 SQLite 保存作品、版本、残片、转录、段落、异文、注释、修订层和快照，并通过 `http.server` 暴露 JSON API。

## 启动与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8114`，地址 <http://127.0.0.1:8114>。首次启动创建一个带缺页残片和不可辨标记的示例。数据库可通过 `COLLATION_DB` 指定，端口可通过 `PORT` 指定。

## 业务规则

- 版本类型限定为 `version`、`fragment`、`transcription`。
- 段落和版本必须属于同一作品，同一版本不能重复对齐同一段落。
- 只有负责人或被单独授权的编辑可以修改对应版本；其他用户只有查看权限。
- `[缺页]`、`[不可辨]`、`[残损]` 等标记会参与校勘稿导出和缺口统计，不匹配的方括号会拒绝保存。
- 每次新增或修改异文都会产生递增修订号和 JSON 快照；提交必须携带 `expected_revision`，旧页面不能覆盖新层。
- 锁定段落由负责人执行，锁定后任何新修订都会被拒绝。

## 封存与授权移交闭环

- **封存**：负责人对段落执行封存时，留存该段落的异文、注释与对齐快照（`seals` 表，含 `basis_hash`）。封存后已交付内容（底本、对齐、异文）不可改动，但注释仍可补录；锁定段落仍按原规则拒绝任何修订。
- **封存依据**：底本与对齐文本共同构成封存依据。修改底本（`POST /api/passages/{id}/basis`）或改定对齐（`POST /api/alignments/update`）后，旧封存立即失效（状态置为 `invalid`），导出与缺口统计按新依据重算。
- **修订确认**：异文修订须由负责人确认后才进入交付稿；未确认修订只出现在导出的 `pending_variants` 中，不混入交付稿。确认只对当前修订生效。
- **转交**：负责人转交作品时，未处理修订与封存责任一并交给接手人，作品 `owner_id` 变更，原负责人失去写权限（审阅权限降为查看）。接手人须具备该作品的审阅权限或为版本编辑，否则拒绝转交。
- **幂等**：转交与修订确认均须携带 `idempotency_key`，同一键并发或重复提交只成功一次并重放原结果；作品已转交后原负责人再次提交会被拒绝。
- **历史查阅**：`GET /api/works/{id}/seals`、`GET /api/works/{id}/handovers`、`GET /api/passages/{id}/history` 分别查阅封存、转交与段落修订/封存/确认历史。

## 主要接口

- `POST /api/users`、`POST /api/works`
- `POST /api/works/{id}/witnesses`、`POST /api/witnesses/{id}/editors`
- `POST /api/works/{id}/passages`、`POST /api/works/{id}/access`
- `POST /api/alignments`、`POST /api/alignments/update`
- `POST /api/variants`、`POST /api/variants/{id}/revisions`、`POST /api/variants/{id}/confirm`
- `POST /api/passages/{id}/lock`、`POST /api/passages/{id}/seal`、`POST /api/passages/{id}/basis`
- `POST /api/works/{id}/handovers`
- `GET /api/passages/{id}/snapshots/{revision}?user_id=...`
- `GET /api/works/{id}/collation?user_id=...`
- `GET /api/works/{id}/seals?user_id=...`、`GET /api/works/{id}/handovers?user_id=...`
- `GET /api/passages/{id}/history?user_id=...`

导出接口把版本对齐、已确认异文、注释、残损缺口和封存状态组合成可复核的交付稿；未确认修订单列于 `pending_variants`。

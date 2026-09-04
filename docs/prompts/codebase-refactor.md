# Codebase Refactor Prompt

Dùng prompt này khi yêu cầu coding agent quét codebase của repo `hermes-structured-runs-plugins` và refactor một khu vực lớn (finalizer, session-settle, media serving, event proxy, state persistence...).

## Ngôn ngữ

- Agent phải trả lời, báo cáo, giải thích và đặt câu hỏi cho user bằng **tiếng Việt**.
- Giữ nguyên tiếng Anh cho: code, comment trong source, tên file/route/class/function/env var, câu lệnh shell, log và trích dẫn nguyên văn từ tool output.
- Thuật ngữ kỹ thuật không có từ tiếng Việt phổ biến (boundary, caller, upstream, blast radius, breaking change, traversal...) được giữ nguyên, phần diễn giải xung quanh vẫn bằng tiếng Việt.
- Ngoại lệ có chủ đích: các chuỗi prompt gửi cho Hermes agent / finalizer trong source (ví dụ `final_output_check_prompt` và `instructions` của `complete_structured` trong `_finalize.py`) hiện được viết bằng tiếng Việt vì phục vụ người dùng Việt. Giữ nguyên quy ước này khi refactor; **không** dịch chúng sang tiếng Anh. Comment giải thích code xung quanh vẫn viết bằng tiếng Anh.

## Chế độ auto-scan khi chưa có yêu cầu cụ thể

Nếu prompt không mô tả task cụ thể, thực hiện theo thứ tự sau:

1. Kiểm tra `git status` và `git diff` của worktree (cả staged lẫn unstaged).
2. Nếu có diff chưa commit, review chính xác các thay đổi đó và các caller / endpoint / env var / consumer của state file bị ảnh hưởng.
3. Nếu worktree sạch, xác định branch hiện tại và base branch phù hợp, sau đó review diff của branch so với base branch (`main` hoặc merge-base).
4. Nếu đang ở `main` và không có branch diff, thực hiện codebase health scan tổng quát: cấu trúc plugin, dead code, duplicated logic, boundary wrapper/core, security model (auth passthrough, media roots, traversal), xử lý lỗi upstream, race condition trong finalizer, độ phủ test và maintainability.
5. Chỉ đưa ra findings có evidence từ code; không tự sửa code khi chưa có yêu cầu hoặc xác nhận phạm vi.

Không dùng `git reset`, `git checkout`, `git clean` hoặc thao tác làm mất thay đổi của user trong quá trình scan.

```text
Trả lời toàn bộ bằng tiếng Việt (code, tên định danh, lệnh shell và log giữ nguyên tiếng Anh).

Nếu tôi không nêu task cụ thể, trước tiên hãy chạy `git status` và kiểm tra cả staged/unstaged `git diff`.
Nếu worktree sạch, kiểm tra branch hiện tại:
- branch khác `main`: review diff của branch so với `main` hoặc merge-base phù hợp;
- đang ở `main` và không có branch diff: thực hiện health scan tổng quát trên codebase.
Chỉ báo cáo findings có evidence; không tự sửa code trong chế độ này.

Hãy quét toàn bộ codebase trước khi sửa code.

## Mục tiêu

- Vấn đề cần giải quyết: [mô tả cụ thể]
- Kết quả kiến trúc mong muốn: [mô tả boundary/module/luồng mới]
- Phạm vi: [hàm, endpoint, env var hoặc phần state file liên quan]

## Quy trình bắt buộc

1. Đọc `README.md`, `structured-runs/plugin.yaml`, toàn bộ package `structured-runs/` (`__init__.py`, `_config.py`, `_state.py`, `_session_db.py`, `_schema.py`, `_media.py`, `_upstream.py`, `_finalize.py`, `_app.py`) và các test trong `tests/` (loader chung ở `tests/_plugin.py`).
2. Tìm toàn bộ điểm liên quan: route (`app.router.add_*` trong `_app.py`), env var (`STRUCTURED_RUNS_*` trong `_config.py`), field của state file `~/.hermes/structured_runs_state.json`, truy vấn vào Hermes `state.db` (`sessions`, `messages`, `async_delegations` trong `_session_db.py`), và mọi hàm helper module-level được test import trực tiếp.
3. Phân tích dependency, coupling, execution flow (create → poll/SSE → session settle → post-completion check → finalizer → media enrich) và blast radius trước khi sửa.
4. Báo cáo ngắn gọn:
   - kiến trúc hiện tại;
   - vấn đề tìm thấy;
   - kiến trúc đề xuất;
   - file dự kiến thay đổi;
   - rủi ro và breaking changes (đặc biệt với HTTP contract, hình dạng JSON trả về, và schema của state file).
5. Chưa sửa code cho đến khi tôi xác nhận phạm vi, trừ khi tôi nói rõ là được tự động triển khai.

## Quy tắc refactor

- Ưu tiên thay đổi nhỏ, theo từng phase có thể kiểm chứng.
- Giữ nguyên behavior ngoài phạm vi.
- **Không** patch hoặc thay thế Hermes core. Plugin này chỉ là wrapper trên port `8646` forward tới API server thật (`STRUCTURED_RUNS_UPSTREAM`, mặc định `:8642`); run vẫn phải chạy qua agent loop và tool thật của Hermes.
- Khi rename hoặc move function / helper / route / env var:
  - migrate toàn bộ caller, test và tài liệu (`README.md`);
  - xóa symbol cũ hoàn toàn;
  - không tạo alias, shim, wrapper, re-export hoặc redirect compatibility.
  - nếu đổi tên env var hoặc field state file, nêu rõ đây là breaking change và cách migrate.
- Giữ nguyên security model:
  - mọi call wrapper phải dùng cùng `Authorization: Bearer ...` như upstream;
  - không trả structured result đã cache khi upstream trả `401` / `403`;
  - không persist `Authorization` header vào state file;
  - media serving: caller phải có auth hợp lệ, `run_id` phải known, `path` phải khớp field `*_path` trong parsed JSON, file phải nằm dưới `STRUCTURED_RUNS_MEDIA_ROOTS`, chặn `../` traversal.
- Không làm hỏng tính idempotent / an toàn concurrency của finalizer: giữ `_STATE_LOCK`, cờ `structured_done` / `structured_status == "running"`, và việc persist terminal snapshot trước khi finalize.
- Không đưa logic không liên quan (ví dụ business rule của Hermes core) vào wrapper.
- Bảo toàn các thay đổi đang có trong worktree; không reset hoặc overwrite code không liên quan.
- Nếu thay đổi HTTP route, hình dạng response, tên event SSE (`structured.completed` / `structured.failed` / `structured.skipped`), hoặc schema state file, phải nêu rõ breaking change và tác động trước khi thực hiện.
- Comment trong source code phải viết bằng tiếng Anh.

## Cách triển khai

- Cập nhật contract trước (chữ ký hàm, hình dạng payload, tên env var), sau đó implementation, callers và tests.
- Sau mỗi phase, chạy test focused liên quan và kiểm tra import sạch.
- Không commit nếu chưa được yêu cầu. Khi được yêu cầu commit, tuân theo quy ước attribution của repo.

## Verification bắt buộc

Chạy các check nhỏ phù hợp trong lúc làm, sau đó chạy full suite:

```bash
# Cú pháp và import sạch (cần aiohttp + jsonschema trong môi trường)
python3 -m py_compile structured-runs/*.py

# Import package đúng cách Hermes core load (submodule_search_locations + __package__)
python3 -c "import importlib.util, sys; d='structured-runs'; spec=importlib.util.spec_from_file_location('structured_runs', d+'/__init__.py', submodule_search_locations=[d]); m=importlib.util.module_from_spec(spec); m.__package__='structured_runs'; m.__path__=[d]; sys.modules['structured_runs']=m; spec.loader.exec_module(m); print('import ok')"

# Unit tests (loader chung ở tests/_plugin.py)
python3 -m unittest discover -s tests -v
```

> Nếu `python3` hệ thống thiếu `aiohttp` / `jsonschema` và không có `pip`, chạy test qua `uv`:
> `uv run --with aiohttp --with jsonschema python -m unittest discover -s tests -v`

Nếu môi trường thiếu `aiohttp` / `jsonschema`, cài trước khi verify:

```bash
python3 -m pip install aiohttp jsonschema
```

Smoke test thủ công khi có Hermes gateway chạy:

```bash
curl http://localhost:8646/health
# tạo một structured run nhỏ theo ví dụ trong README.md, rồi poll tới khi completed
```

## Báo cáo cuối

Viết bằng tiếng Việt, trình bày ngắn gọn:

- kiến trúc sau refactor;
- file đã thay đổi;
- route / response shape / tên event SSE / env var / schema state file đã đổi;
- breaking changes hoặc migration notes;
- test và verification đã chạy (kèm output tóm tắt);
- cảnh báo hoặc phần còn lại chưa xử lý.
```

## Ví dụ boundary cho repo này

Khi tách rõ trách nhiệm wrapper vs upstream, có thể thêm đoạn sau vào prompt:

```text
Wrapper (:8646) chỉ được: validate json_schema, forward request kèm header allowlist tới upstream,
chờ session settle, chạy post-completion agent check trong cùng session, gọi finalizer
(`llm.complete_structured` trong `_finalize.py`), enrich media URL, và persist metadata/terminal snapshot.
Wrapper KHÔNG được: tự chạy tool, tự quyết định nội dung câu trả lời, sửa hành vi agent loop,
hay trả kết quả khi upstream auth từ chối.
Mọi field mới thêm vào response phải đi qua `_finalize.merge_structured`; không thêm key vào `parsed`
nếu schema client không khai báo (giữ an toàn với `additionalProperties: false`).
```

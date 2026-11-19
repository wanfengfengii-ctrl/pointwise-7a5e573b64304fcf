# 暗亮参照采集与交班

`README.md` 的原灰度流程继续适用。新增校准轮也使用已登记相机、每台相机唯一的活动轮、定长原帧和成功写入驱动的环槽。相机和会话调用仍按事件串行。

## 开轮和接纳

- 在 `station.runs.session.RecordingSession` 新增 `open_calibrated_run(camera_id, capacity, width, height, threshold)`，返回带 `.run_id` 的轮。容量、宽高及设备占用沿用原规则。阈值接受非负的 Python `int` 或 `fractions.Fraction`，排除布尔值，开轮后固定。
- `session.submit_reference(run_id, kind, reference_sequence, data)` 向运行中的校准轮交一件参照，成功返回 `None`。`kind` 为 `dark` 或 `bright`，`data` 为与该轮原帧等长的 `bytes`。暗亮件共用一条严格递增、允许跳号的整数参照源序，与带材帧源序相互独立，两种件可按任一顺序到达。
- 一轮最多一件待齐参照。待齐时重复交同种件须拒绝。相反的一件到达后，只有所有像素的亮值都严格大于对应暗值，才成对生效并清空待齐件。参照组在轮内以 `(dark_sequence, bright_sequence)` 标识，按暗亮角色排列。同字节而源序不同的原件仍有各自出处。拒收保留原待齐件、当前组、两条已接纳源序的进度及轮转位置。
- 仍用 `session.append_frame` 或原模拟相机的 `step(CameraFrameEvent(...))` 接纳带材帧，沿用原回执。首组生效前拒收带材帧。随后每帧使用实际接纳那一刻的组。参照交件不占带材帧槽，未齐件不影响当前组。换组不改变已接纳帧的归属。采集会话只保留当前生效组、待齐原件及有效帧仍引用的组，原件留存不随换组历史增长。关闭会话后只从归档领取已签件。
- `session.live_calibrated(run_id)` 可在录制中或暂停后查看有效窗，返回 `(frame_count, normalized_total, over_limit_count)`，其中 `normalized_total` 为 `Fraction`。对每个像素求 `(原值 - 暗值) / (亮值 - 暗值)`，将该比值截断到 `[0, 1]` 后再取精确的帧和与窗总量。帧和严格超过开轮阈值才计一次超限。空窗三项为零。例如阈值为 1、暗值 `[20, 20]`、亮值 `[40, 40]` 的一帧 `[19, 41]` 计量为一帧、总量 1、零次超限。

未知轮或相机沿用 `KeyError`，关闭资源沿用 `RuntimeError`。非法参照、非法带材帧、阶段不符或用校准专用入口操作原灰度轮报 `ValueError`。参照只在轮处于运行阶段接纳。

## 暂停签发和固定领取

沿用 `session.pause_run(run_id)` 和 `handover_board`。在 `station.archive.issue` 新增 `issue_calibrated(session, store, run_id, cutoff) -> str`。只接受同轮同截点的非空暂停窗。原帧、逐帧归属、所用暗亮原件、固定口径和计量与交班引用在一次 SQLite 提交中可见。暂停时的当前组或待齐件若没有被窗口成员使用，不随成果交付。`before_commit` 钩子仍可在提交前打断签发。写入或提交失败时异常向调用者传播，其他连接看不到本次成果及引用，原暂停证据和待签单可供重试。同轮同截点成功重投返回原成果号。校准轮经 `issue_stopped` 报 `ValueError`。

在 `station.archive.readback` 新增 `read_calibrated(store, result_id)`。它可在相机和原会话关闭、归档重开后按号领取。成功返回字典，键及值的含义如下。

| 键 | 值 |
| --- | --- |
| `result_id`、`camera_id` | 原成果号和相机名 |
| `cutoff` | 原 `PausePoint` |
| `capacity`、`width`、`height`、`threshold` | 固定轮配置，阈值为 `Fraction` |
| `values` | 按存档原件复算并与固定计量核对后的三元组，格式同 `live_calibrated` |
| `frames` | 按原带材帧源序排列的元组，每项为 `sequence`、`data`、`reference_id` 三键的字典，`data` 为原始 `bytes` |
| `references` | 只含该窗口所用组的字典。键为 `reference_id`，值为 `dark`、`bright` 两键的字典，各自的值为 `(reference_sequence, data)`，`data` 为原始 `bytes` |

V1 成果经 `read_calibrated` 返回 `None`，未知成果号报 `KeyError`。校准成果的帧、参照、绑定或固定口径有缺失或不一致时，抛出 `station.archive.manifest.ArchiveIntegrityError`，整项不交付。即使改绑的另一组拥有相同字节，实际归属仍须核验。保留原灰度接口、原成果和全部原回归。

## 跨组样例与运行

`examples/calibration.json` 给出相机事件和手算检查点。阈值的两项是分子和分母，`pixels` 可按原样转为 `bytes`。`frame` 事件走原相机入口，`reference` 事件走参照入口。带 `reject` 的事件预期报 `ValueError`。每个检查点从新轮重放相应的事件前缀后暂停，`after_events` 计入遭拒的尝试。预期的 `frames` 三元组依次是带材帧源序、所用暗场源序、所用亮场源序，`values` 的中间两项表示总量的分子和分母。

在事件 12 后，新组已生效，但只有帧 10、20 所用的两组原件进入暂停成果。再收事件 13 的轮会覆盖帧 10，帧 20 和 30 使用的两组原件字节相同，原参照序分别为 `(3, 4)` 与 `(7, 8)`。

基线的 `.pointwise-runtime.json` 仍运行原灰度回归及演示。交付时沿用其中的版本和类型，保留 `setup` 的准备命令数组列表以及 `verify`、`demo` 的单命令 argv 格式。让 `verify` 继续运行原回归，并核验新模式的成对接纳、覆盖、签发失败续办及重开复核。让 `demo` 用给定事件展示跨组采集、覆盖及停源重开领取的真实归属和计量。核验或演示失败沿用非零退出码。

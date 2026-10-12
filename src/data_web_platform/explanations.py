"""Explain evidence without modifying severity, thresholds or raw messages."""
import re

ISSUES = {
    "observation_gap": ("真实反馈存在缺口", "相邻真实观测间隔超过阈值，部分输出点需要跨越缺失区间。另一个手没动不会导致反馈理应消失。", "核对topic、实测gap_ms和threshold_ms；检查采集/通信。不要把缺失默认当成静止。"),
    "video_gap": ("所需画面已经过旧", "对齐时只能取不晚于输出时刻的最近视频帧，某些点的画面年龄超过阈值。输出FPS高于相机FPS导致重复帧本身不一定异常。", "查看视频topic、受影响时段和帧年龄；优先检查相机录制是否停顿。"),
    "command_coverage_gap": ("所有控制命令同时失效", "单路关节或夹爪命令可用同帧state补齐，但这里四路控制在同一输出时刻都没有有效命令，无法确认受控轨迹。", "核对遥操作会话和控制topic。命令年龄参数只影响旧目标保持，不恢复真实观测。"),
    "command_pause_filled": ("控制暂停已补对应反馈", "某路控制未发布或目标过期，已经用该帧对应state填action；这是允许的处理说明，常见于单臂操作。", "无需仅因此删除轨迹。确认另一控制仍有效，并理解这部分action代表保持当前姿态。"),
    "stationary_edges_trimmed": ("已裁去静止首尾", "基于关节/夹爪反馈的端点位移裁剪，所有字段与视频保持相同行范围；中间暂停仍保留。", "查看首尾裁去帧数与秒数。若需保留静止示例，可关闭首尾裁剪后重新检查转换。"),
    "numeric_read_error": ("数值信号或录制契约无法读取", "可能涉及缺失topic、消息schema、非有限数值、时间戳、夹爪标定或坐标系；此类问题不等于另一个手没动。", "展开原始错误定位实际字段与topic；补齐采集元数据或修正采集端，不单靠调大时间阈值。"),
    "video_read_error": ("视频消息无法读取", "视频MCAP/metadata可能缺失，或者消息格式与时间戳不符合当前录制契约。", "核对video目录和metadata中引用文件，确认录制已完整保存；保留原始错误供排查。"),
    "video_decode_error": ("视频码流或帧映射损坏", "读取消息并不意味着视频可解码。完整检查发现坏包、帧数/顺序/尺寸等问题，无法保证与机器人信号对应。", "定位原始错误与受影响录制，检查原视频；不可通过关闭仅检查的deep-video绕过正式转换检查。"),
    "alignment_error": ("无法建立有效对齐轨迹", "共同区间、观测缺口、控制覆盖或活动范围不满足规则；具体原因需结合原始错误和同轨迹其他问题。", "优先查看同轨迹的observation_gap、video_gap或command_coverage_gap。若明确为全静止，可按任务决定是否关闭首尾裁剪。"),
    "timeline_error": ("时序诊断未能完成", "检查时间关系时发生错误，当前报告不能证明这条轨迹已正确对齐。", "核对时间戳类型、顺序和原始异常；不要将缺少详细统计理解为通过。"),
    "inconsistent_video_dimensions": ("同批相机尺寸不一致", "该录制视频尺寸与此批基准不同，不能直接写入同一个固定图像shape的数据集。", "按相同分辨率分批转换，或在明确的媒体处理流程统一尺寸后再组织数据。"),
}


def explain_issue(issue):
    title, meaning, next_step = ISSUES.get(issue.get("code"), (
        "需要复核的检查项", "当前版本尚未为这个检查代码提供专门解释；请以原始报告为准。", "结合topic、时间范围、阈值和原始信息判断，不能仅凭文字猜测根因。"))
    severity = str(issue.get("severity", "info")).lower()
    effect = {"error": "转换时跳过此录制；不会因此放宽规则或删除源数据。",
              "warning": "警告通常不会单独阻止转换，但需复核数据质量；最终以轨迹valid判定为准。",
              "info": "这是处理说明，本身不导致轨迹被跳过。"}.get(severity, "以原始严重级别和valid结果为准。")
    message = str(issue.get("message", "")).lower()
    if issue.get("code") == "numeric_read_error":
        if "calibrat" in message or "motor_min" in message or "motor_max" in message:
            meaning = "夹爪归一化依赖录制时的min/max电机行程标定。标定缺失、范围无效或不一致会让开合度失真。"
        elif "frame_id" in message or "frame id" in message:
            meaning = "传感器或末端消息的坐标系标识不符合thor-v1契约，直接混用会改变力/位姿含义。"
        elif "timestamp" in message or "clock" in message:
            meaning = "数值信号的时间戳或时钟关系不满足读取要求，无法保证所有信号处在可比较的时间轴。"
    facts = []
    for key, label, unit in (("gap_ms", "实测间隔", "ms"), ("threshold_ms", "允许阈值", "ms"),
                             ("affected_frames", "受影响主帧", "帧"), ("affected_samples", "受影响采样", "点"),
                             ("sampling_fps", "检查网格", "Hz")):
        if isinstance(issue.get(key), (int, float)):
            facts.append(f"{label} {issue[key]:g} {unit}")
    if issue.get("topic"):
        facts.insert(0, f"信号：{issue['topic']}")
    return {"title": title, "meaning": meaning, "evidence": "；".join(facts), "effect": effect, "next_step": next_step}


def explain_diagnostic(message, severity="WARN"):
    """Known Doctor/validator patterns; never invent a repaired/healthy verdict."""
    text = str(message).lower()
    if re.search(r"\b(?:nan|inf)\b|non.finite", text):
        meaning = "报告涉及非有限数值。此类数值可能传播到训练损失，需要核对具体字段和episode；修复副本不等于恢复真实采样。"
    elif "zero variance" in text or "constant" in text or "frozen" in text:
        meaning = "信号长时间不变。单臂静止、保持夹爪也会出现此现象；同时也可能是反馈卡住，需结合视频与任务判断。"
    elif "stats.json" in text or "normalization" in text or "zero std" in text:
        meaning = "归一化统计缺失或不满足上游要求。v2.1可能只有逐episode统计；缺少聚合stats.json不等于原数值丢失。核对训练读取器需要的统计形式。"
    elif "jump" in text or "sudden" in text:
        meaning = "动作相邻变化相对该数据的变化尺度较大，可能是控制跳变或拼接问题，也可能是正常快速动作。核对具体episode与视频；单凭统计阈值不能确定真实原因。"
    elif "shape" in text or "dimension" in text:
        meaning = "字段形状或维度需要复核。wrench_windows本来就是[N,12]；检查实际Parquet值与info.json声明，不能只为消除提示而丢掉高频维度。"
    elif "timestamp" in text or "monotonic" in text or "dropped frame" in text:
        meaning = "时间戳、帧间隔或连续性不满足诊断规则。需核对数据FPS和具体episode，不应直接把未知时间段填成静止。"
    elif "urdf" in text or "skipped" in text:
        meaning = "此项可能因缺少适用模型或条件而跳过。跳过不代表做过完整验证，尤其不能视为硬件运动学或安全验证。"
    elif "minimum" in text or "maximum" in text or "saturat" in text:
        meaning = "部分取值集中在边界。可能是正常夹爪开闭/限位，也可能是饱和或裁切；查看具体维度及任务，不自动删除。"
    elif "short" in text or "chunk" in text or "length" in text:
        meaning = "轨迹长度可能不足所选策略/chunk要求，或长度声明不一致。核对具体条目，调整训练范围与采集长度，不补造帧。"
    else:
        meaning = "这是上游诊断结论。当前没有足够结构化证据自动确定根因，请保留原始信息并结合字段、episode和参数复核。"
    return {"title": "如何理解这条诊断", "meaning": meaning,
            "effect": "是否使任务失败取决于操作及fail_on门槛；原数据未自动修改。",
            "next_step": "展开原始信息定位具体对象。涉及fix/trim时先确认参数，只生成新的副本。"}


def annotate_report(report):
    for item in report.get("results", []):
        for issue in item.get("issues", []):
            issue["explanation"] = explain_issue(issue)
    for check in report.get("checks", []):
        for message in check.get("messages", []):
            if message.get("severity", "PASS") != "PASS":
                message["explanation"] = explain_diagnostic(message.get("message"), message.get("severity"))
    return report


def annotate_job(job):
    """Decorate an API snapshot, not the persisted provider evidence."""
    if isinstance(job.get("check_report"), dict):
        annotate_report(job["check_report"])
    annotate_report({"results": job.get("check_results", [])})
    result = job.get("result") or {}
    if isinstance(result.get("report"), dict):
        annotate_report(result["report"])
    validation = job.get("validation") or result.get("validation") or {}
    job["validation_explanations"] = [
        {"message": message, "severity": level, "explanation": explain_diagnostic(message, level)}
        for key, level in (("warnings", "WARN"), ("errors", "FAIL")) for message in validation.get(key, [])]
    if job.get("error"):
        job["error_explanation"] = {
            "title": "任务为什么停止",
            "meaning": "请先看逐条检查与验证结果。全异常不会生成数据集；磁盘、编码器或源文件变化等运行故障也会停止，不能当作可跳过的录制异常。",
            "effect": "失败不代表原数据被删除，也不能把准备缓存视为已发布输出。",
            "next_step": "保留报告，核对原始错误和输出是否已发布；解决原因后使用新目录重新提交。"}
    return job

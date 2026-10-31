"""Bounded, auditable shift planning; no employee or labour-rule assumptions hidden."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import math
from pathlib import Path


DEFAULT_SHIFTS = (
    {"name": "夜班", "start": "00:00", "end": "08:00", "required": 6},
    {"name": "早班", "start": "08:00", "end": "16:00", "required": 6},
    {"name": "晚班", "start": "16:00", "end": "00:00", "required": 6},
)


def _number(value, name, low, high, *, integer=True):
    valid = type(value) is int if integer else type(value) in (int, float)
    if not valid or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}必须在 {low}–{high} 之间")
    return value


def _clock(value):
    if not isinstance(value, str) or len(value) != 5:
        raise ValueError("班次时间必须是 HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("班次时间必须是 HH:MM") from exc
    if parsed.second or parsed.microsecond:
        raise ValueError("班次时间必须是 HH:MM")
    return parsed


def _inputs(
    start_date,
    days,
    employee_count,
    employees,
    shifts,
    min_rest_hours,
    max_consecutive_days,
    max_shifts_per_employee,
):
    try:
        start = date.fromisoformat(start_date)
    except (ValueError, TypeError) as exc:
        raise ValueError("排班开始日期必须是 YYYY-MM-DD") from exc
    _number(days, "排班天数", 1, 31)
    _number(employee_count, "员工人数", 1, 200)
    _number(min_rest_hours, "班间休息小时", 0, 48, integer=False)
    _number(max_consecutive_days, "最大连续工作天数", 1, 31)
    _number(max_shifts_per_employee, "每人最多班次数", 1, 31)
    anonymous = employees is None
    if employees is None:
        names = [f"员工{i:02d}" for i in range(1, employee_count + 1)]
    else:
        if not isinstance(employees, list) or len(employees) != employee_count:
            raise ValueError("员工姓名列表数量必须与员工人数一致")
        if any(
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 80
            or any(ord(char) < 32 for char in name)
            for name in employees
        ):
            raise ValueError("员工姓名须为1–80字的可显示文字")
        names = [name.strip() for name in employees]
        if len(set(names)) != len(names):
            raise ValueError("员工姓名或编号必须唯一")
    selected = [dict(item) for item in DEFAULT_SHIFTS] if shifts is None else shifts
    if not isinstance(selected, list) or not 1 <= len(selected) <= 5:
        raise ValueError("请提供1–5个班次")
    normalized = []
    for shift in selected:
        if not isinstance(shift, dict) or set(shift) != {
            "name",
            "start",
            "end",
            "required",
        }:
            raise ValueError("班次需要且仅接受name/start/end/required")
        name = shift["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 30:
            raise ValueError("班次名称须为1–30字")
        begin, end = _clock(shift["start"]), _clock(shift["end"])
        _number(shift["required"], "每班人数", 1, employee_count)
        duration = (
            (end.hour * 60 + end.minute) - (begin.hour * 60 + begin.minute)
        ) % 1440
        if not duration or duration > 12 * 60:
            raise ValueError("单班必须大于0小时且不超过12小时")
        normalized.append({**shift, "name": name.strip()})
    if len({item["name"] for item in normalized}) != len(normalized):
        raise ValueError("班次名称必须唯一")
    normalized.sort(key=lambda item: item["start"])
    per_day = sum(item["required"] for item in normalized)
    if per_day > employee_count:
        raise ValueError("本工具每人每天最多一班；每日需求超过员工人数，无法满足")
    if per_day * days > employee_count * max_shifts_per_employee:
        raise ValueError("总班次数需求超过每人最多班次数所允许的总容量")
    return start, names, normalized, anonymous


def _slots(start, days, shifts):
    result = []
    for offset in range(days):
        current = start + timedelta(days=offset)
        for shift in shifts:
            begin = datetime.combine(current, _clock(shift["start"]))
            end = datetime.combine(current, _clock(shift["end"]))
            if end <= begin:
                end += timedelta(days=1)
            result.append(
                {
                    "date": current.isoformat(),
                    "shift": shift["name"],
                    "start": begin.isoformat(timespec="minutes"),
                    "end": end.isoformat(timespec="minutes"),
                    "required": shift["required"],
                }
            )
    return result


def audit_schedule(schedule):
    """Recompute coverage, overlap/rest and workload from the assignments."""
    names = schedule["employees"]
    constraints = schedule["constraints"]
    assignments = schedule["assignments"]
    by_name = {name: [] for name in names}
    violations = []
    expected = _slots(
        date.fromisoformat(schedule["start_date"]), schedule["days"], schedule["shifts"]
    )
    if len(assignments) != len(expected):
        violations.append("班次记录数量与计划不一致")
    for index, slot in enumerate(expected):
        if index >= len(assignments):
            break
        actual = assignments[index]
        if any(
            actual.get(key) != slot[key]
            for key in ("date", "shift", "start", "end", "required")
        ):
            violations.append("班次日期/时间/需求与计划不一致")
        workers = actual.get("employees", [])
        if len(workers) != slot["required"] or len(set(workers)) != len(workers):
            violations.append(f"{slot['date']} {slot['shift']} 人数不足或重复")
        for name in workers:
            if name not in by_name:
                violations.append("存在未登记员工")
            else:
                by_name[name].append(slot)
    loads = []
    for name, work in by_name.items():
        work.sort(key=lambda item: item["start"])
        dates = [date.fromisoformat(item["date"]) for item in work]
        if len(set(dates)) != len(dates):
            violations.append(f"{name} 同一日期安排了多班")
        if len(work) > constraints["max_shifts_per_employee"]:
            violations.append(f"{name} 班次数超过上限")
        longest = run = 0
        for index, current in enumerate(dates):
            run = (
                run + 1
                if index and current == dates[index - 1] + timedelta(days=1)
                else 1
            )
            longest = max(longest, run)
        if longest > constraints["max_consecutive_days"]:
            violations.append(f"{name} 连续工作天数超过上限")
        rests = []
        for previous, following in zip(work, work[1:]):
            hours = (
                datetime.fromisoformat(following["start"])
                - datetime.fromisoformat(previous["end"])
            ).total_seconds() / 3600
            rests.append(hours)
            if hours < constraints["min_rest_hours"]:
                violations.append(f"{name} 班间休息不足")
        hours = sum(
            (
                datetime.fromisoformat(item["end"])
                - datetime.fromisoformat(item["start"])
            ).total_seconds()
            / 3600
            for item in work
        )
        loads.append(
            {
                "employee": name,
                "shifts": len(work),
                "hours": hours,
                "rest_days": schedule["days"] - len(set(dates)),
                "max_consecutive_days": longest,
                "shortest_rest_hours": min(rests) if rests else None,
            }
        )
    return {
        "passed": not violations,
        "violations": violations,
        "employees": loads,
        "total_assignments": sum(
            len(item.get("employees", [])) for item in assignments
        ),
        "checked_constraints": [
            "每班人数",
            "员工有效且不重复",
            "每天最多一班",
            "最大班次数",
            "班间休息",
            "连续工作天数",
        ],
    }


def plan_schedule(
    *,
    start_date,
    days=7,
    employee_count=30,
    employees=None,
    shifts=None,
    min_rest_hours=12,
    max_consecutive_days=5,
    max_shifts_per_employee=5,
):
    start, names, shifts, anonymous = _inputs(
        start_date,
        days,
        employee_count,
        employees,
        shifts,
        min_rest_hours,
        max_consecutive_days,
        max_shifts_per_employee,
    )
    slots = _slots(start, days, shifts)
    # Deterministic bounded rotations: load-balancing heuristics are not a
    # complete constraint solver. Failure means no plan found within this bound.
    for attempt in range(min(len(names), 40)):
        state = {
            name: {"count": 0, "last_date": None, "last_end": None, "run": 0}
            for name in names
        }
        assignments = []
        for slot in slots:
            day = date.fromisoformat(slot["date"])
            begin, end = (
                datetime.fromisoformat(slot["start"]),
                datetime.fromisoformat(slot["end"]),
            )
            eligible = []
            for index, name in enumerate(names):
                item = state[name]
                if item["count"] >= max_shifts_per_employee or item["last_date"] == day:
                    continue
                if (
                    item["last_end"]
                    and (begin - item["last_end"]).total_seconds()
                    < min_rest_hours * 3600
                ):
                    continue
                run = (
                    item["run"] + 1
                    if item["last_date"] == day - timedelta(days=1)
                    else 1
                )
                if run > max_consecutive_days:
                    continue
                eligible.append(
                    (item["count"], run, (index - attempt) % len(names), name)
                )
            eligible.sort()
            if len(eligible) < slot["required"]:
                break
            selected = [row[-1] for row in eligible[: slot["required"]]]
            for name in selected:
                item = state[name]
                item["run"] = (
                    item["run"] + 1
                    if item["last_date"] == day - timedelta(days=1)
                    else 1
                )
                item["last_date"], item["last_end"] = day, end
                item["count"] += 1
            assignments.append({**slot, "employees": selected})
        if len(assignments) == len(slots):
            result = {
                "schema_version": 1,
                "kind": "schedule",
                "title": "人员排班计划",
                "start_date": start_date,
                "days": days,
                "employees": names,
                "anonymous_example": anonymous,
                "shifts": shifts,
                "constraints": {
                    "min_rest_hours": min_rest_hours,
                    "max_consecutive_days": max_consecutive_days,
                    "max_shifts_per_employee": max_shifts_per_employee,
                    "max_shifts_per_day": 1,
                },
                "assumptions": [
                    "匿名员工示例，须替换为实际人员名单"
                    if anonymous
                    else "按提供的员工名单排班",
                    "所有员工技能相同，无请假或不可用日期输入",
                    "仅检查计划区间内的休息；不包含区间前后的已有班次",
                    "跨午夜班次按开始日期计入当天",
                    "不代表满足未提供的行业、合同或法定排班规定",
                ],
                "assignments": assignments,
            }
            result["audit"] = audit_schedule(result)
            if not result["audit"]["passed"]:
                raise ValueError("生成计划未通过独立约束检查，未输出排班")
            return result
    raise ValueError(
        "在有界排班搜索内未找到满足全部约束的计划；请调整班次需求、人数或休息规则。未生成部分排班冒充完整结果。"
    )


def create_schedule(
    workspace,
    path,
    *,
    start_date,
    days=7,
    employee_count=30,
    employees=None,
    shifts=None,
    min_rest_hours=12,
    max_consecutive_days=5,
    max_shifts_per_employee=5,
    overwrite=False,
):
    """Create an actual workbook and an auditable JSON plan, only after validation."""
    from .office_tools import create_spreadsheet
    from .tools import ToolRegistry

    workspace = Path(workspace).resolve()
    target = ToolRegistry(workspace, None).resolve_path(path)
    if target.suffix.lower() != ".xlsx":
        raise ValueError("排班计划请使用 .xlsx 文件名")
    audit_target = target.with_name(target.name + ".schedule.json")
    ToolRegistry(workspace, None).resolve_path(
        audit_target.relative_to(workspace).as_posix()
    )
    if audit_target.exists() and not overwrite:
        raise ValueError("排班审计文件已存在，默认不覆盖")
    plan = plan_schedule(
        start_date=start_date,
        days=days,
        employee_count=employee_count,
        employees=employees,
        shifts=shifts,
        min_rest_hours=min_rest_hours,
        max_consecutive_days=max_consecutive_days,
        max_shifts_per_employee=max_shifts_per_employee,
    )
    dates = [
        (date.fromisoformat(start_date) + timedelta(days=i)).isoformat()
        for i in range(days)
    ]
    allocated = {
        (name, row["date"]): row["shift"]
        for row in plan["assignments"]
        for name in row["employees"]
    }
    loads = {item["employee"]: item for item in plan["audit"]["employees"]}
    columns = ["员工", *dates, "总班次数", "总小时", "休息天数"]
    rows = [
        [
            name,
            *[allocated.get((name, day), "休息") for day in dates],
            loads[name]["shifts"],
            loads[name]["hours"],
            loads[name]["rest_days"],
        ]
        for name in plan["employees"]
    ]
    # All explicit rules and assumptions are visible as rows in the workbook,
    # not only hidden in the JSON audit attachment.
    rows += [
        [
            "排班规则",
            "；".join(
                f"{item['name']} {item['start']}–{item['end']} 每班{item['required']}人"
                for item in plan["shifts"]
            ),
        ]
        + [""] * (len(columns) - 2),
        [
            "约束",
            f"班间休息≥{min_rest_hours}小时；连续工作≤{max_consecutive_days}天；每人≤{max_shifts_per_employee}班",
        ]
        + [""] * (len(columns) - 2),
    ]
    rows += [
        ["假设", assumption] + [""] * (len(columns) - 2)
        for assumption in plan["assumptions"]
    ]
    import shutil
    import tempfile
    from .office_tools import _atomic_bundle

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".jarvis-schedule-", dir=target.parent
    ) as temporary:
        staged_root = Path(temporary)
        result = create_spreadsheet(
            staged_root,
            target.name,
            title=f"{employee_count}人排班计划（{start_date}起{days}天）",
            columns=columns,
            rows=rows,
            overwrite=False,
        )
        writers = {}
        for artifact in result["artifacts"]:
            staged = staged_root / artifact["path"]
            destination = target.parent / staged.name
            writers[destination] = lambda output, staged=staged: shutil.copyfile(
                staged, output
            )
            artifact["path"] = destination.relative_to(workspace).as_posix()
            if "preview_path" in artifact:
                artifact["preview_path"] = (
                    (target.parent / Path(artifact["preview_path"]).name)
                    .relative_to(workspace)
                    .as_posix()
                )
        payload = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        writers[audit_target] = lambda output: output.write_bytes(payload)
        _atomic_bundle(writers, overwrite=overwrite)
    result["path"] = target.relative_to(workspace).as_posix()
    result["preview_path"] = (
        (target.parent / Path(result["preview_path"]).name)
        .relative_to(workspace)
        .as_posix()
    )
    result["artifacts"].append(
        {
            "path": audit_target.relative_to(workspace).as_posix(),
            "size": audit_target.stat().st_size,
            "media_type": "application/json",
        }
    )
    result["schedule"] = {
        "start_date": start_date,
        "days": days,
        "employee_count": employee_count,
        "anonymous_example": plan["anonymous_example"],
        "assumptions": plan["assumptions"],
    }
    result["audit"] = plan["audit"]
    return result

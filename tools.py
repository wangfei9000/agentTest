"""估值、出报告、查询案例。

三个函数都会交给本地模型当作工具。查询案例和出报告的 URL 为空时返回示例数据。
估值请求云房接口，并把单价、总价映射成页面使用的字段。
"""

from __future__ import annotations

import inspect
import json
import re
import ssl
import threading
import types
import typing
import urllib.error
import urllib.request
from typing import Any, Callable

import certifi

# 三个业务接口。留空时走本文件里的示例数据；填上地址后改为 POST JSON。
# 请求体字段与对应函数参数一致，响应必须是 JSON 对象，字段见各函数上方的注释。
# 失败时返回 {"ok": false, "error": "原因"}。ok 可以省略，省略按成功处理。

# 查询案例。POST 后返回 cases 列表。
SEARCH_CASES_URL = ""
# 估值。POST 云房接口，再映射成页面用的单价、总价和 valuation_id。
VALUATION_URL = "https://dpi.yunfangdata.com/dpi/execute?code=CMBZPEKD4YPB8UHG"
# 出报告。POST 后返回标题、摘要和正文。
GENERATE_REPORT_URL = ""

# 云房估价的固定账号和模式，不交给模型填写。
_DPI_ACCOUNT_ID = "358f5eb811074b81998b10561af618b2"
_DPI_MODE = "自动"
_CITY_RE = re.compile(r"[\u4e00-\u9fff]{2,8}?市")
_PROVINCE_RE = re.compile(r"[\u4e00-\u9fff]{2,8}省")

_lock = threading.Lock()
_valuation_seq = 0
_report_seq = 0
_valuations: dict[str, dict[str, Any]] = {}
_last_valuation_id = ""

_CASES: list[dict[str, Any]] = [
    {
        "id": "C2026-001",
        "community": "望京新城",
        "address": "朝阳区望京新城 3 号楼",
        "district": "朝阳区",
        "area": 89.2,
        "price": 6850000,
        "unit_price": 76794,
        "deal_date": "2026-08-12",
        "property_type": "住宅",
    },
    {
        "id": "C2026-002",
        "community": "望京新城",
        "address": "朝阳区望京新城 8 号楼",
        "district": "朝阳区",
        "area": 91.0,
        "price": 7020000,
        "unit_price": 77143,
        "deal_date": "2026-06-03",
        "property_type": "住宅",
    },
    {
        "id": "C2026-003",
        "community": "望京花园",
        "address": "朝阳区望京花园 2 号楼",
        "district": "朝阳区",
        "area": 78.5,
        "price": 5960000,
        "unit_price": 75924,
        "deal_date": "2026-07-19",
        "property_type": "住宅",
    },
    {
        "id": "C2026-004",
        "community": "中关村公馆",
        "address": "海淀区中关村公馆 5 号楼",
        "district": "海淀区",
        "area": 102.0,
        "price": 12400000,
        "unit_price": 121569,
        "deal_date": "2026-05-22",
        "property_type": "住宅",
    },
    {
        "id": "C2026-005",
        "community": "金融街融府",
        "address": "西城区金融街融府 1 号楼",
        "district": "西城区",
        "area": 68.0,
        "price": 9860000,
        "unit_price": 145000,
        "deal_date": "2026-04-02",
        "property_type": "住宅",
    },
    {
        "id": "C2026-006",
        "community": "丰台金茂府",
        "address": "丰台区丰台金茂府 6 号楼",
        "district": "丰台区",
        "area": 120.0,
        "price": 6100000,
        "unit_price": 50833,
        "deal_date": "2026-04-18",
        "property_type": "住宅",
    },
    {
        "id": "C2026-007",
        "community": "通州万达广场",
        "address": "通州区通州万达广场 1 层",
        "district": "通州区",
        "area": 56.0,
        "price": 2240000,
        "unit_price": 40000,
        "deal_date": "2026-03-09",
        "property_type": "商办",
    },
]

_CASES_BY_ID = {item["id"]: item for item in _CASES}

_DISTRICT_UNIT = {
    "朝阳区": 78000,
    "海淀区": 92000,
    "西城区": 110000,
    "东城区": 105000,
    "丰台区": 52000,
    "通州区": 38000,
    "昌平区": 42000,
    "大兴区": 40000,
    "石景山区": 56000,
}

_TYPE_FACTOR = {
    "住宅": 1.0,
    "别墅": 1.35,
    "商办": 0.72,
    "商业": 0.8,
}


# 查询案例接口。
# 请求：keyword（小区或地址，必填）、district（行政区）、area_min / area_max（建筑面积，平方米）。
# 返回：summary；cases 为列表，每条含 id、community、address、district、area、price（总价，元）、
# unit_price（单价，元/平方米）、deal_date、property_type。没有命中时 ok 仍为 true，cases 为空。
# 地址：SEARCH_CASES_URL。为空时使用本文件里的示例案例。
def search_cases(
    keyword: str,
    district: str | None = None,
    area_min: float | None = None,
    area_max: float | None = None,
) -> dict[str, Any]:
    """仅在用户明确要求查询成交案例时使用。用户要求估值时不要调用。

    查询可比成交案例。

    Args:
        keyword: 小区名或地址关键词，例如望京新城
        district: 行政区，例如朝阳区。不确定时留空
        area_min: 最小建筑面积，单位平方米。不确定时留空
        area_max: 最大建筑面积，单位平方米。不确定时留空
    """
    payload = {
        "keyword": keyword,
        "district": district,
        "area_min": area_min,
        "area_max": area_max,
    }
    if SEARCH_CASES_URL:
        return _from_remote(SEARCH_CASES_URL, payload, "查询案例")
    return _mock_search(keyword, district, area_min, area_max)


# 估值接口。用户要求估价时走这里，不要用查询案例代替。
# 云房请求：accountExternalId、cityName、address、area（字符串）、houseType、mode。
# 不传 priceDate、楼层和案例编号。
# 返回：valuation_id、unit_price（元/平方米）、total_price（元）、basis（依据）、summary。
# 地址：VALUATION_URL。为空时按示例单价估算，并把结果记在本进程里，供出报告使用。
def valuation(
    address: str,
    area: float,
    property_type: str = "住宅",
    city_name: str | None = None,
    floor: str | None = None,
    case_ids: list[str] | None = None,
) -> dict[str, Any]:
    """用户要求估值或估价时必须调用本工具，返回评估单价和总价。不要改用查询案例。

    对一处房产做估价，返回单价和总价。

    Args:
        address: 房产地址或小区名，可以包含城市名
        area: 建筑面积，单位平方米
        property_type: 物业类型，例如住宅、别墅、商办。不确定时填住宅
        city_name: 城市，必须来自用户原话，例如通化市。用户没说就留空，不要猜测
        floor: 楼层描述，例如高楼层、中楼层、低楼层。不确定时留空
        case_ids: 已查询到的案例编号列表。没有则留空
    """
    if VALUATION_URL:
        return _dpi_valuation(address, area, property_type, city_name, floor)
    #return _mock_valuation(address, area, property_type, floor, case_ids)


# 出报告接口。
# 请求：subject（评估对象，必填）、valuation_id（不填则用本进程最近一次估值）、
# case_ids（不填则用该次估值引用的案例）。
# 返回：report_id、title、summary（摘要）、content（全文）、valuation_id、case_ids。
# 地址：GENERATE_REPORT_URL。为空时用最近一次估值和案例拼一份示例报告。
def generate_report(
    subject: str,
    valuation_id: str | None = None,
    case_ids: list[str] | None = None,
) -> dict[str, Any]:
    """根据估值和成交案例撰写估价报告。

    Args:
        subject: 评估对象，用地址或小区名加面积描述
        valuation_id: 估值编号。留空则使用最近一次估值
        case_ids: 案例编号列表。留空则使用该次估值引用的案例
    """
    payload = {
        "subject": subject,
        "valuation_id": valuation_id,
        "case_ids": case_ids,
    }
    if GENERATE_REPORT_URL:
        return _from_remote(GENERATE_REPORT_URL, payload, "出报告")
    return _mock_report(subject, valuation_id, case_ids)


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """按函数签名清洗参数后调用工具。失败时返回 ok=false，不向外抛异常。"""
    fn = AVAILABLE.get(name)
    if fn is None:
        return {"ok": False, "error": f"未知工具：{name}"}
    try:
        kwargs = _coerce(fn, arguments or {})
        result = fn(**kwargs)
    except Exception as exc:  # 参数不合法或示例逻辑异常，都交回模型自行修正
        return {"ok": False, "error": str(exc)}
    if not isinstance(result, dict):
        return {"ok": True, "result": result}
    result.setdefault("ok", True)
    return result


def using_sample_data() -> bool:
    return not (SEARCH_CASES_URL and VALUATION_URL and GENERATE_REPORT_URL)


AVAILABLE: dict[str, Callable[..., dict[str, Any]]] = {
    "search_cases": search_cases,
    "valuation": valuation,
    "generate_report": generate_report,
}

# 交给 Ollama 生成工具描述的函数对象，顺序与页面上的三项能力一致
TOOLS_FOR_MODEL = [search_cases, valuation, generate_report]

TOOL_LABELS = {
    "search_cases": "查询案例",
    "valuation": "估值",
    "generate_report": "出报告",
}


def first_city(text: str) -> str:
    """取文本里最先出现的「××市」。省名先去掉，避免「吉林省通化市」被收成一个城市。"""
    stripped = _PROVINCE_RE.sub("", text or "")
    matched = _CITY_RE.search(stripped)
    return matched.group(0) if matched else ""


def _dpi_valuation(
    address: str,
    area: float,
    property_type: str,
    city_name: str | None,
    floor: str | None,
) -> dict[str, Any]:
    address = (address or "").strip()
    city = (city_name or "").strip()
    if city and not city.endswith("市"):
        city += "市"
    address = re.sub(r"[\s,，。；;、]+", " ", address).strip()
    if not address:
        return {"ok": False, "error": "估价需要地址或小区名"}
    if area <= 0:
        return {"ok": False, "error": "建筑面积需要大于 0"}
    if area > 100000:
        return {"ok": False, "error": "建筑面积超出可接受范围"}
    if not city:
        return {"ok": False, "error": "请输入城市，例如：通化市。"}

    property_type = (property_type or "住宅").strip() or "住宅"
    body = {
        "accountExternalId": _DPI_ACCOUNT_ID,
        "cityName": city,
        "address": address,
        "area": _area_text(area),
        "houseType": "住宅",
        "mode": "自动",
    }
    try:
        data = _post_json(VALUATION_URL, body)
    except Exception as exc:
        return {"ok": False, "error": f"估值接口失败：{exc}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "估值接口返回的不是 JSON 对象"}

    status = str(data.get("status") or "")
    if status != "OK":
        detail = data.get("message") or data.get("error") or status or "无状态"
        return {"ok": False, "error": f"估值未成功：{detail}"}
    unit_price = _as_int(data.get("price"))
    if unit_price is None:
        return {"ok": False, "error": "未返回单价"}
    total_price = _as_int(data.get("totalPrice"))
    if total_price is None:
        total_price = int(round(unit_price * area))

    valuation_id = str(data.get("externalId") or "").strip()
    if not valuation_id:
        global _valuation_seq
        with _lock:
            _valuation_seq += 1
            valuation_id = f"V{_valuation_seq:04d}"
    result = {
        "ok": True,
        "valuation_id": valuation_id,
        "address": address,
        "area": area,
        "property_type": property_type,
        "city_name": str(data.get("cityName") or city),
        "floor": (floor or "").strip(),
        "unit_price": unit_price,
        "total_price": total_price,
        "basis": _dpi_basis(data),
        "case_ids": [],
        "references": [],
        "summary": f"评估单价 {unit_price:,} 元/平方米，总价 {total_price:,} 元",
    }
    _remember_valuation(valuation_id, result)
    return result


def _area_text(area: float) -> str:
    if float(area).is_integer():
        return f"{area:.1f}"
    return f"{area:.4f}".rstrip("0").rstrip(".")


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _dpi_basis(data: dict[str, Any]) -> str:
    place = "".join(
        str(data.get(key) or "")
        for key in ("province", "cityName", "districtName", "communityName")
    )
    details = []
    if place:
        details.append(f"匹配到{place}")
    if data.get("buildYear"):
        details.append(f"建成年份 {data['buildYear']}")
    if data.get("priceDate"):
        details.append(f"价格日期 {data['priceDate']}")
    if not details:
        return "自动估价。"
    return "自动估价。" + "，".join(details) + "。"


def _from_remote(url: str, payload: dict[str, Any], label: str) -> dict[str, Any]:
    try:
        data = _post_json(url, payload)
    except Exception as exc:
        return {"ok": False, "error": f"{label}接口失败：{exc}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": f"{label}接口返回的不是 JSON 对象"}
    data.setdefault("ok", True)
    return data


def _post_json(url: str, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
        method="POST",
    )
    # 这台机器自带的 Python 没有系统根证书，用 certifi 的证书包校验 HTTPS。
    context = ssl.create_default_context(cafile=certifi.where())
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
    )
    try:
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code} {detail}") from exc


def _mock_search(
    keyword: str,
    district: str | None,
    area_min: float | None,
    area_max: float | None,
) -> dict[str, Any]:
    keyword = (keyword or "").strip()
    district_text = _norm_district(district or "")
    if not keyword and not district_text:
        return {"ok": False, "error": "请提供小区、地址或行政区"}

    low, high = area_min, area_max
    if low is not None and high is not None and low > high:
        low, high = high, low

    matched = []
    for case in _CASES:
        if not _hit(case, keyword, district_text):
            continue
        if low is not None and case["area"] < low:
            continue
        if high is not None and case["area"] > high:
            continue
        matched.append(case)

    matched.sort(key=lambda item: item["deal_date"], reverse=True)
    if matched:
        summary = f"找到 {len(matched)} 条成交案例"
    else:
        summary = "没有符合条件的成交案例"
    return {
        "ok": True,
        "summary": summary,
        "cases": matched,
        "sample": True,
    }

"""
def _mock_valuation(
    address: str,
    area: float,
    property_type: str,
    floor: str | None,
    case_ids: list[str] | None,
) -> dict[str, Any]:
    address = (address or "").strip()
    if not address:
        return {"ok": False, "error": "估价需要地址或小区名"}
    if area <= 0:
        return {"ok": False, "error": "建筑面积需要大于 0"}
    if area > 100000:
        return {"ok": False, "error": "建筑面积超出可接受范围"}

    property_type = (property_type or "住宅").strip() or "住宅"
    ids = [item for item in (case_ids or []) if item in _CASES_BY_ID]
    references = [_CASES_BY_ID[item] for item in ids]
    factor = _TYPE_FACTOR.get(property_type, 1.0)
    floor_text = (floor or "").strip()
    floor_factor = 1.0
    if "高" in floor_text:
        floor_factor = 1.02
    elif "低" in floor_text:
        floor_factor = 0.98
    elif "顶" in floor_text:
        floor_factor = 0.99

    if references:
        unit = sum(item["unit_price"] for item in references) / len(references)
        unit *= factor * floor_factor
        basis = (
            f"参考 {len(references)} 个成交案例的平均单价，"
            f"并按物业类型「{property_type}」"
        )
        if floor_text:
            basis += f"和楼层「{floor_text}」"
        basis += "做了修正。当前为示例数据。"
    else:
        base = 60000
        for name, price in _DISTRICT_UNIT.items():
            short = name[:-1]
            if name in address or short in address:
                base = price
                break
        if area < 60:
            base *= 1.03
        elif area > 120:
            base *= 0.98
        unit = base * factor * floor_factor
        basis = (
            f"未指定成交案例，按地址所在区域的示例单价估算，"
            f"物业类型为「{property_type}」。当前为示例数据。"
        )

    unit_price = int(round(unit))
    total_price = int(round(unit_price * area))
    global _valuation_seq
    with _lock:
        _valuation_seq += 1
        valuation_id = f"V{_valuation_seq:04d}"
    result = {
        "ok": True,
        "sample": True,
        "valuation_id": valuation_id,
        "address": address,
        "area": area,
        "property_type": property_type,
        "floor": floor_text,
        "unit_price": unit_price,
        "total_price": total_price,
        "basis": basis,
        "case_ids": ids,
        "references": [
            {
                "id": item["id"],
                "address": item["address"],
                "unit_price": item["unit_price"],
                "deal_date": item["deal_date"],
            }
            for item in references
        ],
        "summary": f"评估单价 {unit_price:,} 元/平方米，总价 {total_price:,} 元",
    }
    _remember_valuation(valuation_id, result)
    return result
"""

def _mock_report(
    subject: str,
    valuation_id: str | None,
    case_ids: list[str] | None,
) -> dict[str, Any]:
    subject = (subject or "").strip()
    if not subject:
        return {"ok": False, "error": "出报告需要评估对象"}

    chosen_id = (valuation_id or "").strip() or _last_valuation_id
    record = _valuations.get(chosen_id)
    ids = [item for item in (case_ids or []) if item in _CASES_BY_ID]
    if not ids and record:
        ids = [item for item in record.get("case_ids", []) if item in _CASES_BY_ID]
    cases = [_CASES_BY_ID[item] for item in ids]

    global _report_seq
    with _lock:
        _report_seq += 1
        report_id = f"R{_report_seq:04d}"

    lines = [
        f"{subject}估价报告",
        f"报告编号：{report_id}",
        f"评估对象：{subject}",
        "",
        "一、价值意见",
    ]
    if record:
        lines.extend(
            [
                f"估值编号：{record['valuation_id']}",
                f"评估单价：{record['unit_price']:,} 元/平方米",
                f"评估总价：{record['total_price']:,} 元",
                f"依据：{record['basis']}",
            ]
        )
        summary = (
            f"{subject}的评估单价为 {record['unit_price']:,} 元/平方米，"
            f"总价 {record['total_price']:,} 元。"
        )
    else:
        lines.append("尚未取得估值结果。建议先完成估值，再形成正式价值意见。")
        summary = f"已为{subject}起草报告，但还没有估值结果。"

    lines.extend(["", "二、成交案例"])
    if cases:
        for item in cases:
            lines.append(
                f"- {item['address']}，{item['area']} 平方米，"
                f"单价 {item['unit_price']:,} 元/平方米，"
                f"总价 {item['price']:,} 元，成交日期 {item['deal_date']}（{item['id']}）"
            )
    else:
        lines.append("未附成交案例。")

    lines.extend(
        [
            "",
            "三、说明",
            "本报告由本地示例接口生成，用于打通助手流程。接入业务系统后，正文会改为接口返回内容。",
        ]
    )
    title = f"{subject}估价报告"
    return {
        "ok": True,
        "sample": True,
        "report_id": report_id,
        "title": title,
        "summary": summary,
        "content": "\n".join(lines),
        "valuation_id": record["valuation_id"] if record else "",
        "case_ids": ids,
    }


def _remember_valuation(valuation_id: str, result: dict[str, Any]) -> None:
    global _last_valuation_id
    with _lock:
        _valuations[valuation_id] = result
        _last_valuation_id = valuation_id


def _norm_district(text: str) -> str:
    text = text.strip()
    aliases = ("朝阳", "海淀", "西城", "东城", "丰台", "通州", "昌平", "大兴", "石景山")
    if text in aliases:
        return text + "区"
    return text


def _hit(case: dict[str, Any], keyword: str, district: str) -> bool:
    blob = f"{case['community']}{case['address']}{case['district']}"
    if district and district not in blob and case["district"] not in district:
        return False
    if not keyword:
        return True
    return keyword in blob or case["community"] in keyword or case["district"] in keyword


def _coerce(fn: Callable[..., Any], arguments: dict[str, Any]) -> dict[str, Any]:
    hints = typing.get_type_hints(fn)
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if value is None:
            continue
        if value == "" and param.default is not inspect._empty:
            continue
        kwargs[name] = _cast(name, value, hints.get(name, str))
    return kwargs


def _cast(name: str, value: Any, hint: Any) -> Any:
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        args = [item for item in typing.get_args(hint) if item is not type(None)]
        hint = args[0] if args else str
        origin = typing.get_origin(hint)

    if origin in (list, typing.List):
        return _as_list(value)
    if hint in (float, int):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"参数 {name} 需要是数字") from exc
        if hint is int:
            return int(round(number))
        return number
    if hint is str:
        return str(value).strip()
    return value


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]

"""Resolve Korean time expressions locally before retrieving chat evidence."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime  # Exclusive; both boundaries are fixed for the whole request.
    label: str


_DATE = re.compile(
    r"(?<!\d)(?:(?P<y>\d{4})\s*(?:년\s*|[-./])(?P<m>\d{1,2})\s*(?:월\s*|[-./])(?P<d>\d{1,2})(?:\s*일)?"
    r"|(?P<km>\d{1,2})\s*월\s*(?P<kd>\d{1,2})\s*일"
    r"|(?P<sm>\d{1,2})/(?P<sd>\d{1,2}))(?!\d)"
)
_JOIN = r"\s*(?:부터|[~～–—-]|에서|to)\s*"
_NUMBERS = {"하루": 1, "이틀": 2, "사흘": 3, "나흘": 4, "닷새": 5, "엿새": 6, "일주일": 7, "한 주": 7}
_DAY_WORD = re.compile(r"그저께|그제|어제|오늘|(?<!\d)(\d+)\s*일\s*전")


def parse_period(text: str, now: datetime, retention_days: int) -> TimeWindow | None:
    """Return None only when no supported time expression is present."""
    text = re.sub(r"https?://\S+|<#\d+>", "", text)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end = None
    prefix = ""
    dates = list(_DATE.finditer(text))
    if dates:
        if len(dates) > 2 or (len(dates) == 2 and not re.fullmatch(_JOIN, text[dates[0].end():dates[1].start()])):
            raise ValueError("날짜는 하루 또는 '9월 29일~10월 1일'처럼 연속된 범위로 지정해 주세요.")

        def date_value(match, base=None):
            year = int(match['y']) if match['y'] else (base.year if base else now.year)
            month = int(match['m'] or match['km'] or match['sm'])
            day = int(match['d'] or match['kd'] or match['sd'])
            try:
                value = midnight.replace(year=year, month=month, day=day)
                if not match['y']:
                    if base and value < base and month < base.month:
                        value = value.replace(year=year + 1)
                    elif not base and (value - midnight).days > 180:
                        value = value.replace(year=year - 1)
                return value
            except ValueError:
                raise ValueError("존재하지 않는 날짜입니다. 연·월·일을 확인해 주세요.") from None

        start = date_value(dates[0])
        last = date_value(dates[-1], start) if len(dates) == 2 else start
        if len(dates) == 1:
            short_end = re.match(_JOIN + r"(\d{1,2})\s*일", text[dates[0].end():])
            if short_end:
                try:
                    last = start.replace(day=int(short_end[1]))
                except ValueError:
                    raise ValueError("날짜 범위의 마지막 날을 확인해 주세요.") from None
            elif re.match(r"\s*(?:부터|이후)\s*(?:(?:지금|현재|오늘)(?:까지)?)?", text[dates[0].end():]):
                last = midnight
        if last < start:
            raise ValueError("시작 날짜는 끝 날짜보다 늦을 수 없습니다.")
        end = last + timedelta(days=1)
    else:
        relative_days = list(_DAY_WORD.finditer(text))
        if relative_days:
            if len(relative_days) > 2 or (len(relative_days) == 2 and not re.fullmatch(_JOIN, text[relative_days[0].end():relative_days[1].start()])):
                raise ValueError("기간을 하나의 범위로 지정해 주세요. 예: '어제부터 오늘까지'.")
            def day_value(match):
                offset = int(match[1]) if match[1] is not None else {"오늘": 0, "어제": 1, "그제": 2, "그저께": 2}[match[0]]
                if offset > retention_days:
                    raise ValueError("지정한 날짜가 대화 보관기간을 벗어납니다.")
                return midnight - timedelta(days=offset)
            start = day_value(relative_days[0])
            last = day_value(relative_days[-1])
            if len(relative_days) == 1 and re.match(r"\s*(?:부터|이후)\s*(?:(?:지금|현재)(?:까지)?)?", text[relative_days[0].end():]):
                last = midnight
            if last < start:
                raise ValueError("시작 날짜는 끝 날짜보다 늦을 수 없습니다.")
            end = last + timedelta(days=1)
        elif re.search(r"이번\s*주|지난\s*주|저번\s*주", text):
            start = midnight - timedelta(days=midnight.weekday())
            if re.search(r"지난\s*주|저번\s*주", text):
                end = start
                start -= timedelta(days=7)
            else:
                end = now
        else:
            duration = re.search(r"(?:최근|지난|직전)\s*(\d+)\s*(분|시간|일|주)(?!일)", text)
            spoken = re.search(r"(?:최근|지난|직전)\s*(하루|이틀|사흘|나흘|닷새|엿새|일주일|한\s*주)", text)
            duration = duration or re.search(r"(?<!\d)(\d+)\s*(분|시간|일|주)\s*(?:동안|간)", text)
            spoken = spoken or re.search(r"(하루|이틀|사흘|나흘|닷새|엿새|일주일|한\s*주)\s*(?:동안|간)", text)
            if duration:
                amount = int(duration[1])
                minutes = amount * {"분": 1, "시간": 60, "일": 1440, "주": 10080}[duration[2]]
            elif spoken:
                amount = _NUMBERS[re.sub(r"한\s*주", "한 주", spoken[1])]
                minutes = amount * 1440
            else:
                # Explicit but unsupported periods must not silently become 24h.
                if re.search(r"내일|모레|이번\s*달|지난\s*달|최근|직전|[월화수목금토일]요일|\d+\s*(?:년|월|일|시|분)\s*(?:부터|까지)|\d{4}[-./]\d", text):
                    raise ValueError("기간을 해석하지 못했습니다. '오늘', '어제', '최근 3일', '2026-09-30' 또는 '9월 29일~10월 1일'로 지정해 주세요.")
                return None
            if not 1 <= minutes <= 7 * 1440:
                raise ValueError("요약 기간은 1분 이상, 최대 7일로 지정해 주세요.")
            end = now
            start = now - timedelta(minutes=minutes)
            prefix = f"최근 {minutes // 60}시간 · " if minutes % 60 == 0 else f"최근 {minutes}분 · "

    if re.search(r"오전|오후", text):
        if end - start != timedelta(days=1):
            raise ValueError("오전·오후는 '어제 오전'처럼 하루와 함께 지정해 주세요.")
        if '오전' in text and '오후' in text:
            raise ValueError("오전 또는 오후 중 하나를 지정해 주세요.")
        if '오전' in text:
            end = start + timedelta(hours=12)
        else:
            start += timedelta(hours=12)
    # Reject clock expressions until they have explicit clock-range semantics.
    if re.search(r"\d+\s*시(?!간)|\d{1,2}:\d{2}", text):
        raise ValueError("시각 범위는 아직 지원하지 않습니다. '오늘 오전' 또는 '최근 2시간'으로 지정해 주세요.")
    if start >= now:
        raise ValueError("미래의 대화는 조회할 수 없습니다.")
    if end - start > timedelta(days=7):
        raise ValueError("한 번에 조회할 기간은 최대 7일입니다.")
    cutoff = now - timedelta(days=retention_days)
    if start < cutoff:
        raise ValueError(f"요청 기간이 보관기간 {retention_days}일을 벗어납니다. 시작 날짜를 늦춰 주세요.")
    end = min(end, now)
    label = prefix + f"{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M} ({now.tzinfo}, 종료 시각 미포함)"
    return TimeWindow(start.astimezone(timezone.utc), end.astimezone(timezone.utc), label)


def resolve_period(text: str, history: list[dict], retention_days: int,
                   timezone_name: str, now: datetime, *, default_hours: int | None = 24) -> TimeWindow | None:
    local_now = now.astimezone(ZoneInfo(timezone_name))
    turns = [item['content'] for item in history if item.get('role') == 'user'] + [text]
    for turn in reversed(turns):
        period = parse_period(turn, local_now, retention_days)
        if period is not None:
            return period
    if default_hours is None:
        return None
    return parse_period(f"최근 {default_hours}시간", local_now, retention_days)

"""报告和读取共用的分钟窗口；输入为显式时区内的本地时间。"""

import re
from datetime import datetime, timedelta, timezone


def minute_window(start_time, end_time, zone):
    def parse(value):
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}', value):
            raise ValueError('请填写起止时间，格式为 YYYY-MM-DD HH:MM。')
        try:
            local = datetime.fromisoformat(value)
            candidates = {local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc) for fold in (0, 1)
                          if local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == local}
        except (ValueError, OverflowError):
            raise ValueError('起止时间无效，请重新选择。') from None
        if len(candidates) != 1:
            raise ValueError('该时间在所选时区不存在或因夏令时重复，请选择其他时间或改用 UTC。')
        return candidates.pop()
    start, end = parse(start_time), parse(end_time)
    if end <= start:
        raise ValueError('结束时间必须晚于开始时间。')
    return start, end


def describe_window(start, end, zone, zone_name='Asia/Shanghai', precise=False):
    first, stop = start.astimezone(zone), end.astimezone(zone)
    last = (end - timedelta(microseconds=1)).astimezone(zone)
    label = f'{first:%Y-%m-%d %H:%M} → {stop:%Y-%m-%d %H:%M}' if precise else f'{first.date()} → {last.date()}'
    return dict(start=start.isoformat(), end=end.isoformat(), start_date=first.date().isoformat(), end_date=last.date().isoformat(),
                days=(last.date()-first.date()).days+1, label=label, timezone=zone_name,
                precision='minute' if precise else 'day', end_exclusive=True,
                start_time=first.strftime('%Y-%m-%dT%H:%M'), end_time=stop.strftime('%Y-%m-%dT%H:%M'))

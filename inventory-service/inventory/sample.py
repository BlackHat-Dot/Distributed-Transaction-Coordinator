from datetime import datetime
from zoneinfo import ZoneInfo
from datetime import timedelta

local_time = datetime.now(ZoneInfo("Asia/Kolkata")) - timedelta(minutes=5)
print(local_time)
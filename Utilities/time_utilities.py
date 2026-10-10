"""Verbatim port of DOVER_UI/Utilities/time_utilities.py - no FreeSimpleGUI dependency,
no DOVER_UI-specific state. Used by the Dover Controller window's status bar (same
timestamp format as DOVER_UI's own update_status_bar) and GojiScheduler's temperature log.
"""
import time
from datetime import datetime


def date_time_now_string() -> str:
    now = datetime.now()
    return now.strftime("%Y/%m/%d, %H:%M:%S")


def execution_time_stamp() -> str:
    ts = date_time_now_string()
    return ts + ' - '


def execution_date_stamp() -> str:
    date_time = datetime.fromtimestamp(time.time())
    str_date = date_time.strftime("%Y-%m-%d")
    return str_date


def execution_time_now_stamp() -> str:
    date_time = datetime.fromtimestamp(time.time())
    str_time = date_time.strftime("%H:%M:%S")
    return str_time

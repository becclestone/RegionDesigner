from IsMsgPy.IsMsg import TIsMsg
from Constants import implementation_constants as ic


def create_safemon_cmd(reason: int, tbr: dict) -> TIsMsg:
    cmd: TIsMsg = TIsMsg.create_cmd_msg(ic.cACTION_NOTIFICATION_MSG, ic.SAFE_MON_TARGET)
    payload: dict = {ic.cACTION_REASON_PARAM: reason}
    if tbr is not None:
        cmd.add_reply_tbr(tbr)
    cmd.add_msg_payload(payload)
    return cmd

def safemon_action_snap(tbr: dict = None):
    cmd: TIsMsg = create_safemon_cmd(ic.cACTION_SNAP_V, tbr)
    cmd.send_q_destroy()

def safemon_action_scan(tbr: dict = None):
    cmd: TIsMsg = create_safemon_cmd(ic.cACTION_SCAN_V, tbr)
    cmd.send_q_destroy()

def safemon_action_autofocus(tbr: dict = None):
    cmd: TIsMsg = create_safemon_cmd(ic.cACTION_AUTO_FOCUS_V, tbr)
    cmd.send_q_destroy()

def safemon_action_focus(tbr: dict = None):
    cmd: TIsMsg = create_safemon_cmd(ic.cACTION_FOCUS_V, tbr)
    cmd.send_q_destroy()

def safemon_action_laser_on(tbr: dict = None):
    cmd: TIsMsg = create_safemon_cmd(ic.cACTION_LASER_ON_V, tbr)
    cmd.send_q_destroy()

def safemon_action_laser_off(tbr: dict = None):
    cmd: TIsMsg = create_safemon_cmd(ic.cACTION_LASER_OFF_V, tbr)
    cmd.send_q_destroy()


def safemon_action_motion(tbr: dict = None):
    cmd: TIsMsg = create_safemon_cmd(ic.cACTION_MOTION_V, tbr)
    cmd.send_q_destroy()

def safemon_action_camera_light_off(tbr: dict = None):
    cmd: TIsMsg = create_safemon_cmd(ic.cCAMERA_LIGHT_OFF_V, tbr)
    cmd.send_q_destroy()

def safemon_action_idle(tbr: dict = None):
    cmd: TIsMsg = create_safemon_cmd(ic.cACTION_IDLE_V, tbr)
    cmd.send_q_destroy()


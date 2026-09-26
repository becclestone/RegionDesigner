"""Minimal, FreeSimpleGUI-free re-implementation of the one DOVER_UI/Utilities/utilities.py
helper this project needs. utilities.py itself imports FreeSimpleGUI at module level and
pulls in a large amount of unrelated GUI-tab code, which this standalone Qt tool has no
reason to depend on - so only the one function is reproduced here, against TIsMsg directly.
"""
from Constants import implementation_constants as ic
from IsMsgPy.IsMsg import TIsMsg


def move_to_snap_position():
    msg = TIsMsg.create_cmd_msg(ic.cMOVE_TO_IMAGE_SNAP_POS_MSG, ic.CTL_TARGET)
    msg.send_q_destroy()

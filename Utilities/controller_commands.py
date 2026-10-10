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


def move_to_load_position():
    """Mirrors DOVER_UI/Utilities/utilities.py's move_to_load_position - System Config
    tab's "Move to Load Position" button."""
    msg = TIsMsg.create_cmd_msg(ic.cMOVE_TO_LOAD_POS_MSG, ic.CTL_TARGET)
    msg.send_q_destroy()


def move_to_scan_position():
    """Mirrors DOVER_UI/Utilities/utilities.py's move_to_scan_position - System Config
    tab's "Move to Scan Position" button."""
    msg = TIsMsg.create_cmd_msg(ic.cMOVE_TO_SCAN_POS_MSG, ic.CTL_TARGET)
    msg.send_q_destroy()


def move_to_snap_position_motion_only():
    """Mirrors DOVER_UI/Utilities/utilities.py's move_to_snap_position_motion_only -
    System Config tab's "Move to Snap Position" button: pure motion, cMOVE_TO_SNAP_POS_MSG,
    NOT the same message as move_to_snap_position() above (cMOVE_TO_IMAGE_SNAP_POS_MSG,
    which also arms the camera light + triggers a capture for the "Snap Image" toolbar
    button) - do not conflate the two."""
    msg = TIsMsg.create_cmd_msg(ic.cMOVE_TO_SNAP_POS_MSG, ic.CTL_TARGET)
    msg.send_q_destroy()

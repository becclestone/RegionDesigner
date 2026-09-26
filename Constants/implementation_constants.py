from Constants.project_constants import (MOVES_TAB, MANUAL_MOVES_TAB, SECTION_MOVES_TAB, MEMS_SETUP_TAB, LASER_SETUP_TAB,
                               FOCUS_TEST_TAB, AUTO_FOCUS_TAB, STAGE_CALIBRATION_TAB, SYSTEM_CONFIG_TAB, PATH_TAB,
                               PATH_WINDOW_KEY, IMAGE_WINDOW_KEY)

GUI_TARGET = "dover_ui"
CTL_TARGET = "dover_ctl"
RECONSTRUCTION_TARGET = "recon"
IMAGE_CAPTURE_TARGET = "iCapture"
SAFE_MON_TARGET = "safemon"
STITCHER_TARGET = "stitch"
STAINER_TARGET = "stain"
FOCUS_TARGET = "hf"

cTAB_SENDER = "cTS-"  # the tab or window issuing command
# TBRs -> to be returned automatically includes this information in the reply
cMOVES_TAB_TBR: dict = {cTAB_SENDER: MOVES_TAB}
cMANUAL_MOVES_TAB_TBR: dict = {cTAB_SENDER: MANUAL_MOVES_TAB}
cSECTION_MOVES_TAB_TBR: dict = {cTAB_SENDER: SECTION_MOVES_TAB}
cLASER_MOVES_TAB_TBR: dict = {cTAB_SENDER: LASER_SETUP_TAB}
cLASER_SETUP_TAB_TBR: dict = {cTAB_SENDER: MEMS_SETUP_TAB}
cFOCUS_TEST_TAB_TBR: dict = {cTAB_SENDER: FOCUS_TEST_TAB}
cAUTO_FOCUS_TAB_TBR: dict = {cTAB_SENDER: AUTO_FOCUS_TAB}
cSTAGE_CALIBRATION_TAB_TBR: dict = {cTAB_SENDER: STAGE_CALIBRATION_TAB}
cSYSTEM_CONFIG_TAB_TBR: dict = {cTAB_SENDER: SYSTEM_CONFIG_TAB}
cPATH_WINDOW_TBR: dict = {cTAB_SENDER: PATH_WINDOW_KEY}
cIMAGE_WINDOW_TBR: dict = {cTAB_SENDER: IMAGE_WINDOW_KEY}
#
# ##########################################
cEXECUTION_TERMINATED = 'TerminatedBF'
#
# communication msg from STITCHER_TARGET
cRECONSTRUCTED_IMAGE_COMPLETED_MSG = "!ric$"
# payload
cIMAGE_PATH_PARAM = "!ipP"
cIMAGE_CATEGORY_PARAM = "!icP"
cIS_RESUMED_SCAN_PATH = "isRsp"
cIS_RESUME_DIRECTORY_NAME = "!rdp#"
# values for image category param
SC_IMAGE_T = 1
SC_SS_IMAGE_T = 2
NR_IMAGE_T = 3
R_IMAGE_T = 4
#
# basic communication msgs between UI () GUI_TARGET and safemon (SAFE_MON_TARGET)
cACTION_NOTIFICATION_MSG = "aNm"
cACTION_REASON_PARAM = "arp"
# Reasons which need safety monitor action ie. the value of cACTION_REASON_PARAM
cACTION_SNAP_V = 1
cACTION_SCAN_V = 2
cACTION_AUTO_FOCUS_V = 3
cACTION_FOCUS_V = 4
cACTION_MOTION_V = 5
cACTION_LASER_ON_V = 6
cACTION_LASER_OFF_V = 7
cACTION_IDLE_V = 8
cCAMERA_LIGHT_OFF_V = 9

# IsMsgPy command message types sent from DOVER-UI to dover_ctl
# comm msgs from the controller
cPOSITION_UPDATE_MSG = "PosUp"
# fields
cX_POSITION_READ = "xPos"
cY_POSITION_READ = "yPos"
cZ_POSITION_READ = "zPos"

# UI commands
cDISCOVER_STAGES_MSG = "discoverDover"
# reply fields
#
cRESET_STAGE_MSG = "reset"          # moves to load position
#
# Motion msg for camera
cMOVE_TO_IMAGE_SNAP_POS_MSG = "mIS"
cMOVE_TO_SNAP_POS_MSG = "-IS"       # just motion, not a preparation for snap
cMOVE_TO_LOAD_POS_MSG = "mSN"
cMOVE_TO_SCAN_POS_MSG = "mSC"
# command from UI
cSNAP_MSG = "snap"
# comm confirmation about image being available
cIMAGE_READY_MSG = "IR+"
cIMAGE_PATH = "$ip+"
#
# Start Z move msg and params
cSTART_Z_MOVE_MSG = "stZ1"

cZ_SPEED_VAL = "zSp"
# Start X stage move
cSTART_X_MOVE_MSG = "stX1"
cX_SPEED_VAL = "xSp"
# Start Y Stage move
cSTART_Y_MOVE_MSG = "stY1"
cY_SPEED_VAL = "ySp"
# Compound moves - all stages moving together
cSTART_COMPOUND_MOVE_MSG = "stCM"
# reusing z and x constants from above
cX_ALT_SPEED_VAL = "xALTs"
cY_ALT_SPEED_VAL = "yALTs"
cZ_ALT_SPEED_VAL = "zALTs"
#
# Programmed moves
# re-using z and x constants from above for z & speed
cX_MOVE_PM_MSG = "xmPM"
cY_MOVE_PM_MSG = "ymPM"
cZ_MOVE_PM_MSG = "zmPM"
cMOVE_PM_MSG = "mPM"
# fields
cX_POS_PM = "xpPM"
cY_POS_PM = "ypPM"
cZ_POS_PM = "zpPM"
cZO_POS = "zOp"
# Programmed moved reply fields
cPOSITIONS = "xyzPM"
cPOSITION_ERRORS = "errXYZ"
cTIME_VAL = "-tXYZ"
#
# Path moves
# re-using z and x constants from above for z & speed
cCANCEL_PATH_MOVES_MSG = "CPM"
cPATH_LOAD_FRAGMENT = "plF"
cPATH_MSG = "mvPa"
cX_MULTIPLIER = "xMul"
cY_MULTIPLIER = "yMul"
cZ_MULTIPLIER = "zMul"
cPATH_SCAN_TYPE = "pSt"
cPATH = "$Path"
cIS_LUCAS_PATH = "isLuP"
cOUTLIER_REMOVAL = "!or!"
cIGNORE_TIMING = "$it$"
cRUN_SCAN_TRACE = "$rst$"
cSELECTED_MOTION_PROFILE = "$smP"
cSELECTED_INSTRUMENT_PROFILE = "$sIp"
cSCAN_ACCELERATION = "#sa"
cSCAN_JERK = "#sj"
cFOCUS_ACCELERATION = "#fa"
cFOCUS_JERK = "#fj"
cJOG_ACCELERATION = "#ja"
cJOG_JERK = "#jj"
#
cPATH_UPDATE_MSG = "uPa"
cPATH_EXECUTION_TIME = "$fpet"

cPATH_FOCUS_ON_MSG = "@F"
cPATH_FOCUS_OFF_MSG = "#f"
cPATH_SCAN_ON_MSG = "@S"
cPATH_SCAN_OFF_MSG = "#s"
#
cSECTION_ROW = "$sr"
cSECTION_COL = "$sc"
cSECTION_REGION = "$R"
cSECTION_ROW_COL_STR = "!rcs"

# Focus calibration
cZ_AXIS_POSITION_ADJUSTMENT = "+zaa"
# re-using cZ_POSITION for new position to change to
cSTART_FOCUS_ADJUSTMENT = "+sfa"
cZ_POSITION = "*zp"     # z position value to start
cPOSITION_SELECTION = "*ps-"  # true - section, false - absolute position
cMOTION_SELECTION = "*ms"  # focus, scan motion or stationary
# re-using fields
# cX_POS_PM = "xpPM"
# cY_POS_PM = "ypPM"

cSTOP_FOCUS_ADJUSTMENT = "-sfa"
# Controller to UI update event
cUPDATE_Z_POSITION = "-uzp"

# Grid alignment and calibration
cSET_ANCHOR_POINT_MSG = "=sap"
# re-using fields
# cX_POS_PM = "xpPM"
# cY_POS_PM = "ypPM"
# cZ_POS_PM = "zpPM"

cSET_PATH_OFFSET_MSG = "=spo"
# re-using fields
# cSECTION_ROW = "$sr"
# cSECTION_COL = "$sc"
#
# Queries
cGET_CURRENT_POSITION_MSG = "#p#"

# Section move
cSECTION_MOVE_MSG = "$smM"
cSECTION_POSITION = "$sPos"
cSCAN_OFFSET = "$sOff"


# Update COMM fields from controller
cMOVE_UPDATE_MSG = "mUp"
cX_MOVE_UPDATE_MSG = "mUx"
cY_MOVE_UPDATE_MSG = "mUy"
cZ_MOVE_UPDATE_MSG = "mUz"
cSECTION_MOVE_UPDATE_MSG = "mUs"
cPATH_POSITION_UPDATE_MSG = "@ppU"
cPATH_FOCUS_UPDATE_MSG = "@fum"
cPOSITION_ACTION_ORIGIN = "@pAo"

#
# Start MEMS command payload keys
# IsMsgPy command message types sent from MEMS-UI to mems_ctl
# Discover Mems - does not connect to the device, just finds the device and sends information back
cDISCOVER_MEMS = "discoverMEMS"
# Start / Stop MEMS
cSTART_MEMS = "startMEMS"
cRED_LASER_ON = '*rlo'      # just control of the red laser
cSTOP_MEMS = "stopMEMS"
cRESET_START_MEMS = "RstartMEMS"
cRESET_STOP_MEMS = "RstopMEMS"
#
cSTART_OXXIUS_LASER = "OxStart"
cSTOP_OXXIUS_LASER = "OxStop"
cSET_OXXIUS_POWER_MSG = "OxPower"
cDETECTION_LASER_SCAN_POWER_LEVEL = "dPLS"
cDETECTION_LASER_FOCUS_POWER_LEVEL = "dPLF"
cOXXIUS_WARMUP_MSG = "OxW"
cOXXIUS_WARMUP_PARAM = "oxWp"
cOXXIUS_WARMUP_UPDATE_MSG = "@pW"
cOXXIUS_WARMUP_UPDATE_PARAM = "@pWr"
#
cSET_MEMS_RUN_PROFILE_MSG = "*smp"
# reset
cRESET_MEMS = "resetMEMS"
cRESET_MEMS_PARAMS_MSG = "*Rsmp"
#
cLOAD_MEMS_PROFILE_MSG = "*lmp"
#
cTOGGLE_ON_RED_LASER_WO_MEMS = "*rlwm"

#########################################
# reconstruction notifications COMM msgs
cRECONSTRUCTION_ON = "-rON"
cRECONSTRUCTION_OFF = "-rOFF"

#########################################
# auto focus consts
cCALCULATE_AUTOFOCUS_MSG = "*cam"
cCALCULATION_POSITION_TYPE_PARAM = '*ptp'
cABSOLUTE_X_PARAM = '*x'
cABSOLUTE_Y_PARAM = '*y'
cMASTER_SECTION_ROW_PARAM = '*smr'
cMASTER_SECTION_COL_PARAM = '*smc'
cNUMBER_OF_FOCUS_LAYERS_PARAM = '*nfl'
cSTARTING_Z_FOCUS_CALCULATION_PARAM = '*szc'
cFOCUS_CALCULATION_STEP_PARAM = "*zcS"
# msg to start saving focus values after calculation
cSAVE_FOCUS_VALUES_MSG = "*sa"
cSAVE_FOCUS_VALUES_PARAM = "*SAVP"
cFOCUS_SCAN_ONLY_MSG = "*fcO"
cFOCUS_SCAN_ONLY_PARAM = "*fsoP"
cPLANE_COEFFICIENTS_MSG = "*PCM"
cPLANE_COEFFICIENTS_ONLY_PARAM = "*pcp"
#
cCALCULATED_FOCUS_VALUES_MSG = "*fcm"
cPLOT_FOCUS_VALUES_PARAM = '*pfvp'
cCALCULATED_FOCUS_PARAM = '*cfp'
#
# CMD message to stitcher
cKEEP_SECTION_IMAGE_FILES_MSG = "*ksif"
cKEEP_SECTION_IMAGE_FILES_PARAM = "*ksip"
# COMM notification from stitcher
cSTITCHER_COMPLETED_WORK_MSG = "*scw"   # msg tyo UI, only relevant if we run repeated test operation
cCURRENT_WORK_DIRECTORY_MSG = "*cwd*"
cWORK_DIRECTORY_PATH_PARAM = "*wdp*"
# rectangle enclosing scan area, needed for tiff image allocation
cIMAGE_RECTANGLE_SIZE_CMD = "!!is"  # msg to Stitcher
cRECTANGLE_TL_ROW_PARAM = "!r"
cRECTANGLE_TL_COL_PARAM = "!c"
cRECTANGLE_BR_ROW_PARAM = "!R"
cRECTANGLE_BR_COL_PARAM = "!C"
#
# shutter msgs to safemon to control shutter, maybe sent to UI from safemon
cACTION_SHUTTER_CLOSE  = "shCl"
cACTION_SHUTTER_OPEN   = "shOp"
#
#########################################
# System configuration - System tab
cBATCH_PROCESSING_MSG = "-bP-"
cDELETE_CHANNEL_FILES_MSG = "-dCf-"
cMULTI_THREADING_MSG = "-MT-"
cDELETE_BIN_FILES_MSG = "-dBf"
cDELETE_FOCUS_BIN_FILES_MSG = "-dBF"
cUSE_B_CARD_COMPRESSION_MSG = "-cc"
cUSE_BOTH_GAGE_CARDS_MSG = "-ubG-"
cTIFF_DATA_SIZE_MSG = "-tDS"
cBIS_USE_MSG = "-BIS"
cCMF_USE_MSG = "-CMF"
cOVERRIDE_SHUTTER_MSG = "osh"   # to safemon
# bool params for above
cBATCH_PROCESSING_PARAM = "$bpP"                # relevant to main controller and DataTransformer
cDELETE_CHANNEL_FILES_PARAM = "$dcfP"           # relevant to Reconstructor
cDELETE_BIN_FILES_PARAM = "$dbfP"               # relevant to DataTransformer
cDELETE_FOCUS_BIN_FILES_PARAM = "$dbFP"
cUSE_COMPRESSION_PARAM = "uCP"
cMULTI_THREADING_PARAM = "$mtP"                 # relevant to Reconstructor
cUSE_BOTH_GAGE_CARDS_PARAM = "$ubGP"            # relevant to main controller
cTIFF_DATA_SIZE_PARAM = "$tDS"                  # true - int16, false - int8, relevant for Recon and Stitcher
cBIS_PARAM = "-bis"
cCMF_PARAM = "-cmf"
# safemon
cOVERRIDE_SHUTTER_PARAM = "osP"                 # override if true
cOVERRIDE_SHUTTER_POS_PARAM = "spP"             # true - keep opened, false - keep closed
cSHUTTER_POSITION_PARAM = "shPV"                    # true - currently opened, false currently closed
# request for update on internal config
# this is sent from UI to all modules
cSEND_INTERNAL_CONFIG_UPDATE_MSG = "$SIC"
# updates on internal settings
# this is sent to UI from each module
# two scenarios
# 1. send in response to request above from UI
# 2. send on startup - UI may not be running yet
cINTERNAL_CONFIG_VALUES_MSG = "$ICM"
# fields used depend on the sender
#  cBATCH_PROCESSING_PARAM = "$bpP"             - Controller, DataTransformer
#  cDELETE_CHANNEL_FILES_PARAM = "$dcfP"        - Reconstructor
#  cDELETE_BIN_FILES_PARAM = "$dbfP"            - DataTransformer
#  cMULTI_THREADING_PARAM = "$mtP"              - Reconstructor
#  cSHUTTER_POSITION_V    = "shPV"              - safemon
#
# Scan throttle updates
cTHROTTLE_UPDATE_MSG = "thro"
cTHROTTLE_TIME_VALUE = "@tV"
cSCAN_ERROR_MSG = "@SE"
# using consts from shared_rmg_constants defined in is_msg
#      cERROR_STR   "!errS"

# Update MEMS params when active
cUPDATE_Y_VOLTAGE = 'u_YVolt'
cUPDATE_X_AMPLITUDE = 'u_XAmpli'
cUPDATE_X_FREQUENCY = 'u_XFreq'
cUPDATE_SAMPLING_RATE = "u_SR"
cUPDATE_V_DIFFERENCE = "u_VD"


# Start MEMS command payload keys
cENABLE_DIGITAL_OUT = '$edo'
cENABLE_MEMS_DRIVER = '$emd'
cX_SIGNAL_FORM = '$xsf'
cY_VOLTAGE = '$yV'
cX_AMPLITUDE = '$xa'
cX_FREQUENCY = '$xf'
cSAMPLING_RATE = '$sr'
cSAMPLE_POINTS = '$sp'
cCUTOFF = '$co'
cV_DIFFERENCE = '$Vd'

# Focus acquisition configuration
cNO_FOCUS_V = 1
cNORMAL_FOCUS_V = 2
cAUTOFOCUS_V = 3
cAUTOFOCUS_INTERVAL_V = 4
cCALCULATED_FOCUS_PLANE_V = 5
#
cFOCUSING_PARAMS_MSG = "$fcM"
cFOCUSING_TYPE_PARAM = "$fcT"
cFOCUSING_Z_PLANE_PARAM = "$fzP"
cFOCUSING_INTERVAL_PARAM = "$fI"
#
#
# Temperature msg from safemon - COMM
cTEMP_UPDATE_MSG = "!temp"
cTEMP_SENSOR_1 = "!ts1"
cTEMP_SENSOR_2 = "!ts2"
cTEMP_SENSOR_3 = "!ts3"
cTEMP_SENSOR_4 = "!ts4"
cTEMP_SENSOR_5 = "!ts5"
cTEMP_SENSOR_6 = "!ts6"
cTEMP_SENSOR_7 = "!ts7"
#
# recon messages to UI
cRECON_ON_MSG = "!ron"
cRECON_OFF_MSG = "!roff"
# payload is reusing constants
# cSECTION_ROW = "$sr"
# cSECTION_COL = "$sc"
#
# replay payload fields
# error fields below are common and defined in shared_rmq_constants
# cERROR = "!err"
# cERROR_STR = "!errS"
cPORT_NAME = "!port"
cDEVICE_NAME = "!device"
cFIRMWARE_NAME = "!firmware"

# Image capture module
cCAPTURE_IMAGE_NAME = "snap"



# Error Values
# next const is common to all modules
# it is defined in shared_rmq_constants
# cSUCCESS = 0
cDEVICE_NOT_FOUND_MEMS = -1
cDEVICE_NOT_FOUND_STAGES = -2
cUNABLE_TO_CONNECT_MEMS = -3
cUNABLE_TO_CONNECT_STAGES = -4
cUNABLE_TO_SET_PARAMS_MEMS = -5
cDEVICE_ALREADY_RUNNING_MEMS = -6
cUNABLE_TO_SET_DEVICE_TO_ORIGIN = -7
cFAILED_TO_START_DEVICE = -8
cINITIALIZATION_EXCEPTION_DOVER = -9
cPRODUCT_UNDEFINED_DOVER = -10
cNOT_GANDER_STAGES = -11
cMEMS_PROFILE_NOT_LOADED = -12



# error strings ???????


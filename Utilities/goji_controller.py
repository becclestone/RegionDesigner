"""Verbatim port of DOVER_UI/Utilities/goji_controller.py - direct Modbus-TCP client
for the Amplitude/GOJI detection laser controller, no RabbitMQ/IsMsgPy involved. No
FreeSimpleGUI dependency, no DOVER_UI-specific global state - every function opens its
own ModbusTcpClient context manager per call.

Caveat (not a portability issue, a usage one): every function here does blocking
network I/O and connects fresh each call - callers (dover_controller/lasers_tab.py,
goji_scheduler.py) must run these from a worker thread, never the GUI thread, or an
unreachable laser controller freezes the whole UI for the connection timeout.
"""
from pymodbus.client.tcp import ModbusTcpClient
from Utilities.time_utilities import execution_time_stamp

# Configuration
LASER_IP = "10.0.2.40"
MODBUS_PORT = 502

LASER_ON_COIL = 0

LASER_AMPLIFIER_REGISTER = 0

LASER_PULSE_FREQUENCY_REGISTER = 1

LASER_TEMPERATURE_REGISTER = 2


def get_goji_state(client) -> int:
    confirm = client.read_coils(address=LASER_ON_COIL, count=1)
    # confirm ensures response was not None.
    # confirm.bits ensures the .bits lists exists.
    # confirm.bits[0] the coil was successfully set
    if confirm and confirm.bits:
        if confirm.bits[0] == 1:
            print(f"{execution_time_stamp()} Laser is -ON-\n")
            return 1
        else:
            print(f"{execution_time_stamp()} Laser is on -Standby-\n")
            return 0
    else:
        print(f"Warning: State mismatch or read failed.")
        return -2

# Laser ON/Standby
def set_laser_state(on: bool, client):
    state = 1 if on else 0

    print(f"{execution_time_stamp()} Sending command to turn laser {'ON' if on else 'to Standby mode'}")
    client.write_coil(address=LASER_ON_COIL, value=state)  # send command to laser

    # reads coil value back to confirm operation was successful
    return get_goji_state(client)


# Read Amplifier Current
def read_laser_amplitude(client) -> str:
    laser_amplitude = client.read_holding_registers(address=LASER_AMPLIFIER_REGISTER, count=1)
    if laser_amplitude and laser_amplitude.registers:
        # print(f"Amplifier current is {laser_amplitude.registers[0]} mA")
        return str(laser_amplitude.registers[0])
    else:
        print(f"Could not read amplifier current")
        return ""


# Read Amplifier Current
def read_laser_frequency(client) -> str:
    laser_frequency = client.read_holding_registers(address=LASER_PULSE_FREQUENCY_REGISTER)
    if laser_frequency and laser_frequency.registers:
        # print(f"Pulse Picker Frequency is {laser_frequency.registers[0]} kHz")
        return str(laser_frequency.registers[0])
    else:
        print(f"Could not read pulse picker frequency")
        return ""


# Read Amplifier Temperature
def read_amplifier_temperature(client) -> str:
    amplifier_temperature = client.read_input_registers(address=LASER_TEMPERATURE_REGISTER)
    if amplifier_temperature and amplifier_temperature.registers:
        # print(f"Amplifier LD Temperature is {amplifier_temperature.registers[0]} °C")
        return str(amplifier_temperature.registers[0])
    else:
        print("Could not read temperature.")
        return ""


def set_goji_on() -> int:
    with ModbusTcpClient(LASER_IP, port=MODBUS_PORT) as client:
        if not client.connect():
            print("Could not connect to the laser controller.")
            return -1
        else:
            return set_laser_state(True, client)

def set_goji_off() -> int:
    with ModbusTcpClient(LASER_IP, port=MODBUS_PORT) as client:
        if not client.connect():
            print("Could not connect to the laser controller.")
            return -1
        else:
            return set_laser_state(False, client)

def get_goji_info() -> tuple[str, str, str]:
    with ModbusTcpClient(LASER_IP, port=MODBUS_PORT) as client:
        if not client.connect():
            print("Could not connect to the laser controller.")
            return "", "", ""
        else:
            temp: str = read_amplifier_temperature(client)
            freq: str = read_laser_frequency(client)
            amp: str = read_laser_amplitude(client)
            return temp, freq, amp

def get_goji_on_status() -> int:
    with ModbusTcpClient(LASER_IP, port=MODBUS_PORT) as client:
        if not client.connect():
            print("Could not connect to the laser controller.")
            return -1
        else:
            return get_goji_state(client)

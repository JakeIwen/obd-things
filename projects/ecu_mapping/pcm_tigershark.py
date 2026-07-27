"""AlfaOBD's ordered ``TIGERSHARK_CUSW`` PCM Plots request catalog.

The owner-supplied APK maps this 193-row presentation catalog to the literal
two-byte request table ``aa.f4353w0``. Five non-adjacent current-vehicle
request/response anchors independently prove the alignment. Catalog membership
is vendor-derived request evidence, not proof that this PCM supports every DID
or that a label/unit is correct for an installed option.

Units intentionally preserve AlfaOBD's source spelling. This module performs
no decoding and opens no CAN, ADB, file, network, or vehicle interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class PlotDefinition:
    order: int
    did: int
    label: str
    unit: str


def _entry(order: int, did: int, label: str, unit: str) -> PlotDefinition:
    return PlotDefinition(order=order, did=did, label=label, unit=unit)


CATALOG = (
    _entry(1, 0x03D6, "Vehicle speed", "km/h"),
    _entry(2, 0x1737, "Front left wheel speed", "km/h"),
    _entry(3, 0x1738, "Front right wheel speed", "km/h"),
    _entry(4, 0x1739, "Rear left wheel speed", "km/h"),
    _entry(5, 0x173A, "Rear right wheel speed", "km/h"),
    _entry(6, 0x0891, "Target cruise speed", "km/h"),
    _entry(7, 0x01D5, "Engine speed", "rpm"),
    _entry(8, 0x0131, "RPM vs. Vehicle Speed Ratio", "RPM/MPH"),
    _entry(9, 0x01D1, "Target idle speed", "rpm"),
    _entry(10, 0x0FA2, "HEV Target Idle Speed", "rpm"),
    _entry(11, 0x0649, "Engine Position", "EngineDeg"),
    _entry(12, 0x06E3, "Estimated gear", "N"),
    _entry(13, 0x06DA, "Current engine torque", "Nm"),
    _entry(14, 0x011C, "AD/C water temperature", "V"),
    _entry(15, 0x011D, "Coolant temperature", "|C"),
    _entry(16, 0x01B9, "Desired PWM Radiator Fan", "%"),
    _entry(17, 0x022A, "Engine oil pressure", "KPa"),
    _entry(18, 0x0331, "Oil pressure sensor", "V"),
    _entry(19, 0x069E, "VVT Oil Pressure", "KPa"),
    _entry(20, 0x069F, "VVT Oil Temperature", "|C"),
    _entry(21, 0x0127, "Outside temperature", "|C"),
    _entry(22, 0x0128, "Ambient Temperature Voltage", "V"),
    _entry(23, 0x0122, "Intake Air Temperature Voltage", "V"),
    _entry(24, 0x0123, "Intake air temperature", "|C"),
    _entry(25, 0x0174, "Intake manifold air pressure", "KPa"),
    _entry(26, 0x01CA, "Air flow measured", "g/sec"),
    _entry(27, 0x0106, "Accumulated air port mass flow", "G"),
    _entry(28, 0x0103, "MAP Volts", "V"),
    _entry(29, 0x0108, "MAP Vacuum", "KPa"),
    _entry(30, 0x0109, "Barometric pressure", "KPa"),
    _entry(31, 0x010A, "P-Ratio MAP/BARO", "N"),
    _entry(32, 0x0453, "Air Pump Relay", "N"),
    _entry(33, 0x0413, "Throttle Blade Position", "%"),
    _entry(34, 0x0188, "Throttle Position Sensor Percent", "%"),
    _entry(35, 0x0408, "Desired Throttle Position Sensor Position", "V"),
    _entry(36, 0x040E, "Accelerator pedal sensor 1 voltage", "V"),
    _entry(37, 0x040F, "Accelerator pedal sensor 2 voltage", "V"),
    _entry(38, 0x0410, "Throttle position track 1", "V"),
    _entry(39, 0x0412, "Throttle position track 2", "V"),
    _entry(40, 0x041D, "Throttle Position Sensor 1 Minimum Volts", "V"),
    _entry(41, 0x041E, "Throttle Position Sensor 2 Minimum Volts", "V"),
    _entry(42, 0x0125, "Battery Temperature", "|C"),
    _entry(43, 0x0126, "Battery Temperature Voltage", "V"),
    _entry(44, 0x019B, "Target Charging Voltage", "V"),
    _entry(45, 0x01A1, "Generator Duty Cycle", "%"),
    _entry(46, 0x019C, "Voltage Sense", "V"),
    _entry(47, 0x019E, "Battery voltage", "V"),
    _entry(48, 0x012B, "CAT Modeled Temperature", "|C"),
    _entry(49, 0x0141, "Closed Loop Timer", "sec"),
    _entry(50, 0x013F, "1/1 O2 Sensor Goal Volts", "V"),
    _entry(51, 0x0143, "1/1 O2 Sensor Volts", "V"),
    _entry(52, 0x0144, "1/1 O2 Volts (0-1)", "V"),
    _entry(53, 0x014F, "1/1 O2 Goal (0-1)", "V"),
    _entry(54, 0x0153, "1/1 Pulse Width O2 Heater", "%"),
    _entry(55, 0x0154, "1/1 O2 Heater Temperature", "|C"),
    _entry(56, 0x04D0, "1/1 O2 Ratio", "%"),
    _entry(57, 0x04D1, "1/1 O2 Ratio Low Threshold", "%"),
    _entry(58, 0x0178, "1/2 O2 Sensor Volts", "V"),
    _entry(59, 0x0179, "1/2 O2 Volts (0-1)", "V"),
    _entry(60, 0x0180, "1/2 Pulse Width O2 Heater", "%"),
    _entry(61, 0x0181, "1/2 O2 Heater Temperature", "|C"),
    _entry(62, 0x0474, "1/2 O2 Low Volt Spec", "V"),
    _entry(63, 0x015B, "2/1 O2 Sensor Goal Volts", "V"),
    _entry(64, 0x015F, "2/1 O2 Sensor Volts", "V"),
    _entry(65, 0x0160, "2/1 O2 Volts (0-1)", "V"),
    _entry(66, 0x016B, "2/1 O2 Goal (0-1)", "V"),
    _entry(67, 0x016D, "2/1 Pulse Width O2 Heater", "%"),
    _entry(68, 0x016E, "2/1 O2 Heater Temperature", "|C"),
    _entry(69, 0x04D3, "2/1 O2 Ratio", "%"),
    _entry(70, 0x04D4, "2/1 O2 Ratio Low Threshold", "%"),
    _entry(71, 0x018B, "2/2 O2 Sensor Volts", "V"),
    _entry(72, 0x018C, "2/2 O2 Volts (0-1)", "V"),
    _entry(73, 0x0194, "2/2 Pulse Width O2 Heater", "%"),
    _entry(74, 0x0195, "2/2 O2 Heater Temperature", "|C"),
    _entry(75, 0x01A2, "Coil 1 Burn Time", "usec"),
    _entry(76, 0x01A3, "Coil 2 Burn Time", "usec"),
    _entry(77, 0x01A4, "Base Spark", "|"),
    _entry(78, 0x067C, "Spark advance", "EngineDeg"),
    _entry(79, 0x01A5, "Spark Advance 1", "|"),
    _entry(80, 0x01A9, "Knock Sensor 1 Volts", "V"),
    _entry(81, 0x01AA, "Knock Sensor 2 Volts", "V"),
    _entry(82, 0x01AE, "ST Knock Retard", "|"),
    _entry(83, 0x01BF, "Desired Purge Current", "mA"),
    _entry(84, 0x01C1, "Actual Purge Current", "mA"),
    _entry(85, 0x01C3, "Purge Duty Cycle", "%"),
    _entry(86, 0x01D6, "Purge Vapor Ratio", "N"),
    _entry(87, 0x01C9, "Purge AirFlow", "g/sec"),
    _entry(88, 0x0509, "Gen Evap Result Spec", "sec"),
    _entry(89, 0x0940, "Evap turn off pressure", "KPa"),
    _entry(90, 0x0941, "Evap turn on pressure", "KPa"),
    _entry(91, 0x0944, "Evap BRK BSTR pressure", "KPa"),
    _entry(92, 0x0945, "Evap cool down time", "sec"),
    _entry(93, 0x0949, "Evap OFF timer", "sec"),
    _entry(94, 0x094A, "Evap run timer", "sec"),
    _entry(95, 0x01E7, "EGR Duty Cycle", "%"),
    _entry(96, 0x01F6, "EGR Flow", "g/sec"),
    _entry(97, 0x0204, "EGR Sensed Volts", "V"),
    _entry(98, 0x034E, "EGR Monitor Test Timer", "usec"),
    _entry(99, 0x04EE, "EGR Fuel Shift Low Spec", "%"),
    _entry(100, 0x04EF, "EGR Fuel Shift High Spec", "%"),
    _entry(101, 0x173F, "Cooled EGR temperature", "|C"),
    _entry(102, 0x1740, "Cooled EGR voltage", "V"),
    _entry(103, 0x022E, "Cranking Injector Pulse Width", "usec"),
    _entry(104, 0x0234, "Injector Pulse Width Cylinder 1", "usec"),
    _entry(105, 0x0235, "Injector Pulse Width Cylinder 2", "usec"),
    _entry(106, 0x0236, "Injector Pulse Width Cylinder 3", "usec"),
    _entry(107, 0x0237, "Injector Pulse Width Cylinder 4", "usec"),
    _entry(108, 0x0238, "Injector Pulse Width Cylinder 5", "usec"),
    _entry(109, 0x0239, "Injector Pulse Width Cylinder 6", "usec"),
    _entry(110, 0x023A, "Injector Pulse Width Cylinder 7", "usec"),
    _entry(111, 0x023B, "Injector Pulse Width Cylinder 8", "usec"),
    _entry(112, 0x02BA, "Current ADAP Cell ID", "N"),
    _entry(113, 0x02BB, "1/1 Short Term ADAP", "%"),
    _entry(114, 0x02BC, "2/1 Short Term ADAP", "%"),
    _entry(115, 0x02BD, "1/1 Long Term ADAP", "%"),
    _entry(116, 0x02BE, "2/1 Long Term ADAP", "%"),
    _entry(117, 0x0351, "Adaptive Memory Factor Bank 1", "%/100"),
    _entry(118, 0x03BA, "1/1 Adaptive Factor", "%"),
    _entry(119, 0x03D0, "2/1 Adaptive Factor", "%"),
    _entry(120, 0x038F, "Short Term Adaptive Range", "N"),
    _entry(121, 0x032E, "1/2 Non Intrusive Timer", "sec"),
    _entry(122, 0x032F, "2/2 Non Intrusive Timer", "sec"),
    _entry(123, 0x0336, "Fuel Rich Limit Timer Bank 1", "sec"),
    _entry(124, 0x0390, "Switch Time to Close", "sec"),
    _entry(125, 0x0391, "ESIM Frozen Test Timer", "sec"),
    _entry(126, 0x0414, "ETC Directional Duty Cycle", "%"),
    _entry(127, 0x0209, "S/C Switch Voltage", "V"),
    _entry(128, 0x0415, "S/C Switch Voltage 2", "V"),
    _entry(129, 0x0506, "Large Switch Time Spec", "sec"),
    _entry(130, 0x063E, "Cam Burst", "N"),
    _entry(131, 0x063F, "Cam State", "N"),
    _entry(132, 0x0641, "Cam unlocked timer", "msec"),
    _entry(133, 0x06DE, "Intake Cam 1 Actual Position", "EngineDeg"),
    _entry(134, 0x06A6, "Intake Cam 1 Desired Position", "EngineDeg"),
    _entry(135, 0x06AD, "Intake cam 1", "EngineDeg"),
    _entry(136, 0x06C2, "Intake Cam 1 / Crank Difference", "EngineDeg"),
    _entry(137, 0x06A2, "Intake Cam 1 Duty Cycle", "%"),
    _entry(138, 0x069C, "Intake Cam 1 Cleaning Duty Cycle", "%"),
    _entry(139, 0x08C0, "Intake Cam 1 Test Step Counter", "V"),
    _entry(140, 0x08C1, "Intake Cam 1 Test Step Timer", "g/sec"),
    _entry(141, 0x06DF, "Intake Cam 2 Actual Position", "EngineDeg"),
    _entry(142, 0x06A7, "Intake Cam 2 Desired Position", "EngineDeg"),
    _entry(143, 0x06C3, "Intake Cam 2 / Crank Difference", "EngineDeg"),
    _entry(144, 0x06AF, "Intake cam 2", "EngineDeg"),
    _entry(145, 0x06A3, "Intake Cam 2 Duty Cycle", "%"),
    _entry(146, 0x069D, "Intake Cam 2 Cleaning Duty Cycle", "%"),
    _entry(147, 0x08C2, "Intake Cam 2 Test Step Counter", "N"),
    _entry(148, 0x08C3, "Intake Cam 2 Test Step Timer", "g/sec"),
    _entry(149, 0x06D9, "Exhaust Cam 1 Actual Position", "EngineDeg"),
    _entry(150, 0x06A4, "Exhaust Cam 1 Desired Position", "EngineDeg"),
    _entry(151, 0x0696, "Exhaust Cam 1 Position Error", "EngineDeg"),
    _entry(152, 0x031B, "Exhaust Cam 1 / Crank Difference", "EngineDeg"),
    _entry(153, 0x06A0, "Exhaust Cam 1 Duty Cycle", "%"),
    _entry(154, 0x069A, "Exhaust Cam 1 Cleaning Duty Cycle", "%"),
    _entry(155, 0x08BD, "Exhaust Cam 1 Test Step Timer", "N"),
    _entry(156, 0x06E2, "Exhaust Cam 2 Actual Position", "EngineDeg"),
    _entry(157, 0x06A5, "Exhaust Cam 2 Desired Position", "EngineDeg"),
    _entry(158, 0x0697, "Exhaust Cam 2 Position Error", "EngineDeg"),
    _entry(159, 0x06C1, "Exhaust Cam 2 / Crank Difference", "EngineDeg"),
    _entry(160, 0x06AE, "Exhaust cam 2", "EngineDeg"),
    _entry(161, 0x069B, "Exhaust Cam 2 Cleaning Duty Cycle", "%"),
    _entry(162, 0x06A1, "Exhaust Cam 2 Duty Cycle", "%"),
    _entry(163, 0x08BE, "Exhaust Cam 2 Test Step Counter", "N"),
    _entry(164, 0x08BF, "Exhaust Cam 2 Test Step Timer", "V"),
    _entry(165, 0x0ABE, "Active Exhaust Pressure", "KPa"),
    _entry(166, 0x0ABD, "Active Exhaust Pressure Voltage", "V"),
    _entry(167, 0x0AF0, "Fuel Tank Pressure", "KPa"),
    _entry(168, 0x0B79, "Coolant Three Way Valve Actual Position", "|"),
    _entry(169, 0x0B87, "Coolant Three Way Valve Desired Position", "|"),
    _entry(170, 0x0B9C, "Variable Speed Fuel Pump Actual Rail Pressure", "KPa"),
    _entry(171, 0x0B9D, "Variable Speed Fuel Pump Duty Cycle", "%"),
    _entry(172, 0x0BA0, "Variable Speed Fuel Pump Desired Rail Pressure", "KPa"),
    _entry(173, 0x0BA7, "Variable Speed Fuel Pump Voltage", "V"),
    _entry(174, 0x0224, "Fuel level filtered", "V"),
    _entry(175, 0x0225, "Fuel Level Sensor 1 Voltage", "V"),
    _entry(176, 0x0227, "Fuel Level Percent", "%"),
    _entry(177, 0x0228, "Fuel Tank Vapor Volume", "liters"),
    _entry(178, 0x060A, "Ethanol Percent", "%"),
    _entry(179, 0x0328, "AC Output Current", "A"),
    _entry(180, 0x0329, "AC Hi-Side Pressure", "KPa"),
    _entry(181, 0x01B7, "AC Hi-Side Voltage", "V"),
    _entry(182, 0x0892, "Variable A/C Duty Cycle", "%"),
    _entry(183, 0x0D2A, "CNG Rail Temperature", "|C"),
    _entry(184, 0x0D33, "CNG Tank Pressure", "MPa"),
    _entry(185, 0x0D47, "CNG Rail Pressure", "KPa"),
    _entry(186, 0x0D50, "CNG Tank Level Percent", "%"),
    _entry(187, 0x0DA5, "CNG Fuel Volume Used", "liters"),
    _entry(188, 0xFE18, "Transmission Oil Temperature", "|C"),
    _entry(189, 0xFE11, "Trans Temperature Voltage", "V"),
    _entry(190, 0xFE11, "Line Pressure Sensor", "V"),
    _entry(191, 0xFE62, "Turbine speed", "rpm"),
    _entry(192, 0xFE62, "Output Speed", "rpm"),
    _entry(193, 0xFE62, "Transfer speed", "rpm"),
)


def _by_did() -> MappingProxyType:
    grouped: dict[int, list[PlotDefinition]] = {}
    for definition in CATALOG:
        grouped.setdefault(definition.did, []).append(definition)
    return MappingProxyType(
        {did: tuple(definitions) for did, definitions in grouped.items()}
    )


BY_DID = _by_did()

# These five non-adjacent requests were independently observed on this vehicle
# with matching Alfa labels and establish the catalog/table alignment.
ALIGNMENT_ANCHORS = MappingProxyType(
    {
        0x01D5: "Engine speed",
        0x06DA: "Current engine torque",
        0x011D: "Coolant temperature",
        0x022A: "Engine oil pressure",
        0x069F: "VVT Oil Temperature",
    }
)

# A related profile's EOT pair is deliberately kept outside CATALOG. Its
# presence here does not imply TIGERSHARK_CUSW membership or vehicle support.
RELATED_PROFILE_EOT_CANDIDATES = MappingProxyType(
    {
        0x3159: ("Engine oil temperature", "|C", "u8 - 40"),
        0x315A: ("Oil temperature sensor voltage", "V", "u16be * 0.004888"),
    }
)

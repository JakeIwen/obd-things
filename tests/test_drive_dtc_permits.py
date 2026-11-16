"""The two new purposes retain ordinary one-use, lock and evidence gates."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from projects.vehicle_data import drive_dtc_contract as contract, transmit_permit
from tests.drive_dtc_support import CHANNEL, permit_for, rig


@pytest.mark.parametrize('module', contract.MODULES)
def test_new_purpose_spends_exactly_one_send(monkeypatch, module):
    clock, _sock, _transport, permit = rig(monkeypatch, module)
    purpose = contract.PURPOSES[module]
    assert purpose in transmit_permit.ALLOWED_PURPOSES
    transmit_permit.consume(permit, purpose=purpose, channel=CHANNEL)
    with pytest.raises(transmit_permit.TransmitPermitError, match='already consumed'):
        transmit_permit.consume(permit, purpose=purpose, channel=CHANNEL)
    fresh = permit_for(clock, module)
    transmit_permit.consume(fresh, purpose=purpose, channel=CHANNEL)


@pytest.mark.parametrize('module', contract.MODULES)
@pytest.mark.parametrize('failure', ['purpose', 'channel', 'lock', 'evidence'])
def test_new_purposes_cannot_bypass_existing_authority(monkeypatch, module, failure):
    clock, _sock, _transport, permit = rig(monkeypatch, module)
    purpose = contract.PURPOSES[module]
    channel = CHANNEL
    if failure == 'purpose':
        purpose = transmit_permit.RADAR_ALIGNMENT_FLOW
    if failure == 'channel':
        channel = 'can8'
    if failure == 'lock':
        def lost(*args):
            raise OSError('lease closed')
        monkeypatch.setattr(transmit_permit.diagnostic_safety, 'validate_channel_lock', lost)
    if failure == 'evidence':
        clock.value += transmit_permit.PERMIT_TTL_SECONDS
    with pytest.raises(transmit_permit.TransmitPermitError):
        transmit_permit.consume(permit, purpose=purpose, channel=channel)
    with pytest.raises(transmit_permit.TransmitPermitError, match='already consumed'):
        transmit_permit.consume(permit, purpose=contract.PURPOSES[module], channel=CHANNEL)


def test_no_flow_control_purpose_was_added():
    assert transmit_permit.RADAR_DTC_READ == contract.PURPOSES['radar_acc']
    assert transmit_permit.BCM_DTC_READ == contract.PURPOSES['bcm_ccan']
    assert {name for name in transmit_permit.ALLOWED_PURPOSES if '.dtc_' in name} == set(
        contract.PURPOSES.values())


def test_literal_purposes_equal_the_shared_vocabulary_file():
    vocabulary = json.loads(Path(contract.__file__).with_name('drive_dtc.json').read_text())
    assert {transmit_permit.RADAR_DTC_READ, transmit_permit.BCM_DTC_READ} == set(
        vocabulary['modules'].values())
    assert vocabulary['modules'] == {'radar_acc': transmit_permit.RADAR_DTC_READ,
                                     'bcm_ccan': transmit_permit.BCM_DTC_READ}


def test_permit_module_does_not_load_the_drive_dtc_contract():
    """Every C-CAN helper imports transmit_permit before ready; the DTC JSON must not gate it."""
    probe = ('import sys; import projects.vehicle_data.transmit_permit; '
             'print("projects.vehicle_data.drive_dtc_contract" in sys.modules)')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', probe], cwd=root, capture_output=True,
                            text=True, check=True)
    assert result.stdout.strip() == 'False'

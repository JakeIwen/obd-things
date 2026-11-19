"""Lifecycle facade for the C-CAN display feed and the B-CAN compass/fuel companion."""

from projects.vehicle_data.compass_receiver import CompassReceiver
from projects.vehicle_data.display_receiver import DisplayReceiver


class DisplayReceivers:
    def __init__(self, manager, broker):
        self.cluster = DisplayReceiver(manager, status_reader=broker.status_response,
                                       publish=broker.store_display_frame)
        self.compass = CompassReceiver(manager, status_reader=broker.status_response,
                                      publish=broker.store_display_frame,
                                      invalidate=broker.invalidate_bcan_companion,
                                      invalidate_frame=broker.invalidate_bcan_companion,
                                      tank_tap=getattr(broker, 'observe_fuel_tank_reading', None))

    def status(self):
        return {**self.cluster.status(), 'compass': self.compass.status()}

    def start(self):
        self.cluster.start()
        self.compass.start()

    def stop(self, timeout=2.0):
        self.compass.stop(timeout)
        self.cluster.stop(timeout)

    def pause_passive(self, timeout=2.0):
        return self.cluster.pause_passive(timeout)

    def resume_passive(self):
        self.cluster.resume_passive()

    def pause_bcan_passive(self, timeout=2.0):
        """Release the B-CAN companion's passive leases before the auxiliary helper claims the bus."""
        return self.compass.pause_passive(timeout)

    def resume_bcan_passive(self):
        self.compass.resume_passive()

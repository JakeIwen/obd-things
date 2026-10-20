"""Lifecycle facade for the existing C-CAN display feed and B-CAN compass."""

from projects.vehicle_data.compass_receiver import CompassReceiver
from projects.vehicle_data.display_receiver import DisplayReceiver


class DisplayReceivers:
    def __init__(self, manager, broker):
        self.cluster = DisplayReceiver(manager, status_reader=broker.status_response,
                                       publish=broker.store_display_frame)
        self.compass = CompassReceiver(manager, status_reader=broker.status_response,
                                      publish=broker.store_display_frame,
                                      invalidate=broker.invalidate_compass)

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

"""GPIO servo control. Importing this module never connects to hardware."""
import logger as lg

SERVO_PIN = 27


class Servo:
    def __init__(self, config, wait, log=lg.log):
        import pigpio
        self.pi = pigpio.pi()
        self.pulse = config['SERVO_CLICK']
        self.hold = config['SERVO_SPEED']
        self.wait = wait
        self.log = log
        if not self.pi.connected:
            self.pi.stop()
            raise RuntimeError('Cannot connect to pigpiod. Start the daemon on the Pi first.')

    def set_neutral(self):
        # Zero disables pulses. It does not command a calibrated return position.
        result = self.pi.set_servo_pulsewidth(SERVO_PIN, 0)
        if result < 0:
            raise RuntimeError(f'Unable to disable servo pulses: pigpio error {result}')

    def press_fishing_button(self, action):
        self.wait(0)
        self.log(f'Pressing {action} button...')
        try:
            result = self.pi.set_servo_pulsewidth(SERVO_PIN, self.pulse)
            if result < 0:
                raise RuntimeError(f'Servo press failed: pigpio error {result}')
            self.wait(self.hold)
        finally:
            self.set_neutral()
        self.wait(self.hold)

    def cleanup(self):
        try:
            self.set_neutral()
        finally:
            self.pi.stop()


if __name__ == '__main__':
    import time
    from configuration import load_config
    servo = None
    try:
        servo = Servo(load_config(), time.sleep)
        for _ in range(10):
            servo.press_fishing_button('Testing')
            time.sleep(1)
    finally:
        if servo is not None:
            servo.cleanup()

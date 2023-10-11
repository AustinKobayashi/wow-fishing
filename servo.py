import pigpio
import time
import logger as lg

pi = pigpio.pi()

SERVO_PIN = 27
SERVO_CLICK = 910
SERVO_SPEED = 0.1


def set_neutral():
    pi.set_servo_pulsewidth(SERVO_PIN, 0)


# Function to physically click a keyboard key using the servo
def press_fishing_button(action):
    lg.log('Pressing {} button...'.format(action))

    # Move the servo to press the key at a controlled speed
    pi.set_servo_pulsewidth(SERVO_PIN, SERVO_CLICK)
    time.sleep(SERVO_SPEED)  # Adjust the duration for key press speed

    # Return the servo to its neutral position at a controlled speed
    set_neutral()
    time.sleep(SERVO_SPEED)  # Adjust the duration for key release speed


def cleanup():
    pi.stop()


if __name__ == "__main__":
    try:
        for i in range(10):
            press_fishing_button('Testing')  # Physically click the key (e.g., "a")
            time.sleep(1)

    except Exception as e:
        lg.log('Error: {}'.format(e))
    
    cleanup()

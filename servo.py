import RPi.GPIO as GPIO
import time
import logger as lg

# Set up GPIO (change these pin numbers to match your setup)
SERVO_PIN = 11
GPIO.setmode(GPIO.BOARD)
GPIO.setup(SERVO_PIN, GPIO.OUT)

SERVO_NEUTRAL = 3.7

# Configure servo parameters
servo = GPIO.PWM(SERVO_PIN, 50)  # 50 Hz frequency for most servos
servo.start(SERVO_NEUTRAL)  # Set the servo to its neutral position (adjust as needed)

# Global variable to determine servo speed (adjust as needed)
SERVO_SPEED = 0.1  # Lower valu es make the servo move slower

def set_neutral():
    servo.ChangeDutyCycle(SERVO_NEUTRAL)


# Function to physically click a keyboard key using the servo
def press_fishing_button(action):
    lg.log(f'Pressing {action} button...')

    # Define servo angles for key press and release (adjust as needed)
    press_angle = 20  # Angle to press the key
    release_angle = 0  # Angle to release the key

    # Move the servo to press the key at a controlled speed
    servo.ChangeDutyCycle(SERVO_NEUTRAL + press_angle / 18)
    time.sleep(SERVO_SPEED)  # Adjust the duration for key press speed

    # Return the servo to its neutral position at a controlled speed
    set_neutral()
    time.sleep(SERVO_SPEED)  # Adjust the duration for key release speed

    # Move the servo to release the key at a controlled speed (optional)
    servo.ChangeDutyCycle(SERVO_NEUTRAL + release_angle / 18)
    time.sleep(SERVO_SPEED)  # Adjust the duration for key release speed


def cleanup():
    servo.stop()
    GPIO.cleanup()


if __name__ == "__main__":
    try:
        for i in range(10):
            press_fishing_button('Testing')  # Physically click the key (e.g., "a")
            time.sleep(1)

    except Exception as e:
        lg.log(f"Error: {e}")
    
    cleanup()

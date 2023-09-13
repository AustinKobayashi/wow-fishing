import RPi.GPIO as GPIO
import time
import logger as lg

# Set up GPIO (change these pin numbers to match your setup)
SERVO_PIN = 11
GPIO.setmode(GPIO.BCM)
GPIO.setup(SERVO_PIN, GPIO.OUT)

# Configure servo parameters
servo = GPIO.PWM(SERVO_PIN, 50)  # 50 Hz frequency for most servos
servo.start(2.5)  # Set the servo to its neutral position (adjust as needed)

# Global variable to determine servo speed (adjust as needed)
SERVO_SPEED = 0.5  # Lower values make the servo move slower

# Function to physically click a keyboard key using the servo
def press_fishing_button(action):
    try:
        lg.log(f'Pressing {action} button...')

        # Define servo angles for key press and release (adjust as needed)
        press_angle = 20  # Angle to press the key
        release_angle = 0  # Angle to release the key

        # Move the servo to press the key at a controlled speed
        servo.ChangeDutyCycle(2.5 + press_angle / 18)
        time.sleep(SERVO_SPEED)  # Adjust the duration for key press speed

        # Return the servo to its neutral position at a controlled speed
        servo.ChangeDutyCycle(2.5)
        time.sleep(SERVO_SPEED)  # Adjust the duration for key release speed

        # Move the servo to release the key at a controlled speed (optional)
        servo.ChangeDutyCycle(2.5 + release_angle / 18)
        time.sleep(SERVO_SPEED)  # Adjust the duration for key release speed

    except KeyboardInterrupt:
        pass

    finally:
        # Clean up GPIO resources
        servo.stop()
        GPIO.cleanup()


# Example usage:
if __name__ == "__main__":
    try:
        press_fishing_button('Testing')  # Physically click the key (e.g., "a")

    except Exception as e:
        lg.log(f"Error: {e}")

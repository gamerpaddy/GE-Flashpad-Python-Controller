#include <AccelStepper.h>

const int outputPin = 13;
const int stepPin = 8;
const int dirPin = 9;

// Define stepper motor
AccelStepper stepper(AccelStepper::DRIVER, stepPin, dirPin);

// Variables for stepper configuration
float stepsPerDegree = 100.0; // Adjust this value based on your motor/gearing
float maxSpeed = 1000.0;      // Adjust this value (steps per second)
float acceleration = 500.0;   // Adjust this value (steps per second^2)

String input = "";

void setup() {
  pinMode(outputPin, OUTPUT);
  digitalWrite(outputPin, LOW);
  
  stepper.setMaxSpeed(maxSpeed);
  stepper.setAcceleration(acceleration);
  
  Serial.begin(9600);
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();

    if (c == '\n' || c == '\r') {
      if (input.length() > 0) {
        if (input.startsWith("CW")) {
          float angle = input.substring(2).toFloat();
          long steps = round(angle * stepsPerDegree);
          stepper.move(steps);
          while (stepper.distanceToGo() != 0) {
            stepper.run();
          }
          Serial.println("OK");
        } 
        else if (input.startsWith("CCW")) {
          float angle = input.substring(3).toFloat();
          long steps = -round(angle * stepsPerDegree);
          stepper.move(steps);
          while (stepper.distanceToGo() != 0) {
            stepper.run();
          }
          Serial.println("OK");
        }
        else if (input.startsWith("C")) {
          int duration = input.substring(1).toInt();
          if (duration > 0) {
            digitalWrite(outputPin, HIGH);
            delay(duration);
            digitalWrite(outputPin, LOW);
          }
        }
        input = "";
      }
    } else {
      input += c;
    }
  }
}

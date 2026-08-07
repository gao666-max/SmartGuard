/**
 * 智巡哨兵 - STM32 双舵机云台固件
 *
 * 协议: 串口 115200 bps, 换行符结束
 *   "P90\n"  → 水平舵机转到 90°
 *   "T45\n"  → 俯仰舵机转到 45°
 *   回复 "OK\r\n"
 *
 * 接线:
 *   水平舵机 信号线 → PA0 (TIM2_CH1)
 *   俯仰舵机 信号线 → PA1 (TIM2_CH2)
 *   VCC → 5V, GND → GND
 *
 * 烧录: Arduino IDE 选 STM32F103C8 (Blue Pill) 或你的具体型号
 */

#include <Servo.h>

Servo panServo;   // 水平旋转
Servo tiltServo;  // 俯仰

String cmd = "";

void setup() {
  Serial.begin(115200);

  panServo.attach(PA0);
  tiltServo.attach(PA1);

  // 初始位置
  panServo.write(90);
  tiltServo.write(45);

  Serial.println("READY");
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmd.length() > 0) {
        handleCmd(cmd);
        cmd = "";
      }
    } else {
      cmd += c;
    }
  }
}

void handleCmd(String cmd) {
  char type = cmd.charAt(0);
  int angle = cmd.substring(1).toInt();

  // 限幅
  angle = constrain(angle, 0, 180);

  if (type == 'P') {
    panServo.write(angle);
  } else if (type == 'T') {
    angle = constrain(angle, 0, 90);
    tiltServo.write(angle);
  }

  Serial.println("OK");
}

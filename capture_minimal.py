"""Minimal FlashPad capture: arm the detector, fire the X-ray, save the raw image."""
import serial
import flashpad_acquire as fp

WINDOW = 250_000_000        # detector keeps its window open ~9.5 s
EXPOSE_MS = 250             # X-ray on-time
IMG_PORT = fp.HOST_IMAGE_PORT

# open the trigger first: a CH340 board reboots on open and needs time to start
trigger = serial.Serial("COM22", 9600, timeout=1)

s = fp.FlashPadSession()
s._open_sockets()
s.discover()
s.send_port_setup(host_cmd_port=s.reply_port, host_img_port=IMG_PORT)

# ROE init has to finish before the acquisition script may run
s.download_script(fp.build_script_7_roe_init(), "Script7")
s.execute_script()
s.wait_for_execution_complete(timeout_s=60)

acq = fp.build_generic_script(0, 0, 0, [
    fp.pack_acquisition(type_mode=0, max_expose_time=WINDOW),
    fp.pack_delay(10000),
])
s.download_script(acq, "Script0")
s._open_image_socket(IMG_PORT)       # must be open before the detector starts streaming
s.execute_script()

trigger.write(b"C%d\r\n" % EXPOSE_MS)   # the exposure must land inside the window
s.receive_image(output_path="capture.raw", script_id=0,
                image_port=IMG_PORT, timeout_s=120)

trigger.close()
s._close_sockets()
print("saved capture.raw")

# optimus
send imessage texts and receive iphone notifications, all from your browser.

## what you need
- a raspberry pi or other always-on home server, running python 3 and flask
- an iphone with the shortcuts app and the ntfy app
- tailscale on the pi, your phone, and your computer

## setup
1. clone this repo onto the pi and install flask in a virtual environment
2. copy `moptimus_config.example.json` to `moptimus_config.json`, then set your own random ntfy topic and the contact names you want to be able to text
3. run `python server.py` (or set it up as a systemd service so it's always on)
4. set up the iphone shortcuts (shortcut files coming soon)
5. open the pi's tailscale address on port 5000 in your browser

## security
optimus can send texts as you, so treat access to it seriously. the messaging side only accepts requests from your own tailscale devices, and only texts names on your allow list. never expose it to the public internet (for example with tailscale funnel or port forwarding).

## inspiration
optimus stems from my journey to rebuild my relationship with technology. after reading digital minimalism, i realized the importance of working without my phone. this project lets me work without my phone nearby to distract me, and keeps my eyes on one screen so my focus isn't stolen.

## future plans
the original idea for this project came from when i started sleeping without my phone but had no way to check the time or see notifications and texts from friends. next up is an e-ink display that shows that without a screen that pulls me in. long term, i want to eradicate my phone from my deep work sessions completely by bringing even more iphone functionality to my laptop.

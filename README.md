Video Download Organizer
===

Script to automate
* Extrating rar torrents
* Moving/copying tv shows into a sorted location <destination dir>/Series Name/Season #/
* Cleaning up original rar or copied video file when seeding complete

To use, place config.yml in ~/.organize/ and edit the transmission configuration and directory paths to be relevant to your system.

Test run the script. If no errors then add it to conrtab with --cron parameter:

<pathto>/organize.py --cron

Dependencies
----
Requires the following to be installed for python.

	pip3 install -r requirements.txt

echo 'rescue ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/rescue
chmod 0440 /etc/sudoers.d/rescue
visudo -cf /etc/sudoers.d/rescue

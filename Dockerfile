FROM ubuntu:22.04

RUN	apt update && \
	apt install -y --no-install-recommends \
	openssh-server \
	sudo \
	git \
	nano \
	iproute2 \
	vim-tiny \
	curl \
	ca-certificates && \
	apt clean && \
	rm -rf /var/lib/apt/lists/*



RUN sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' /etc/ssh/sshd_config && \
    sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config && \
    echo "AllowUsers developer" >> /etc/ssh/sshd_config

RUN useradd -m -s /bin/bash developer && \
    echo "developer ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/developer

RUN mkdir /var/run/sshd

USER developer
WORKDIR /home/developer

CMD ["sudo", "/usr/sbin/sshd", "-D"]

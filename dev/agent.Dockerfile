# Dev-only image for the agent: Arch (same as CachyOS) + the host tools the agent shells out to.
FROM archlinux:latest
RUN pacman -Sy --noconfirm --needed python ddcutil iproute2 && pacman -Scc --noconfirm

\#!/bin/bash
 # Zatrzymaj skrypt przy pierwszym błędzie

echo "Uruchamianie skryptu konfiguracji maszyny GPU..."

# 1. Instalacja sterowników NVIDIA
echo "Instalowanie sterowników NVIDIA..."
sudo apt update && sudo apt upgrade -y
sudo apt install ubuntu-drivers-common
sudo add-apt-repository ppa:graphics-drivers/ppa -y
sudo apt update
# Sprawdzenie i instalacja rekomendowanego sterownika
RECOMMENDED_DRIVER=$(ubuntu-drivers devices | grep "recommended" | head -1 | awk '{print $3}')
if [ -n "$RECOMMENDED_DRIVER" ]; then
    echo "Instalowanie rekomendowanego sterownika: $RECOMMENDED_DRIVER"
    sudo apt install -y "$RECOMMENDED_DRIVER"
else
    echo "Nie znaleziono rekomendowanego sterownika. Instalowanie domyślnego."
    sudo ubuntu-drivers autoinstall
fi
# sudo reboot   # zakomentowane: reboot zabiłby sesję WSL; sterowniki już zainstalowane
# Skrypt zatrzyma się tutaj. Po restarcie uruchom go ponownie.
echo "System zostanie zrestartowany. Po ponownym uruchomieniu uruchom other-setup.sh"
exit

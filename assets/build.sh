#!/usr/bin/env bash
# Regenerate custom_components/pc_power/brand/*.png from the SVGs here.
# Needs rsvg-convert (librsvg) and ImageMagick (magick). HA serves these locally (2026.x+).
set -euo pipefail
cd "$(dirname "$0")"
OUT=../custom_components/pc_power/brand
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$OUT"

icon() { # name frame-colour size
  sed "s/FRAME/$2/g" icon.svg > "$TMP/$1.svg"
  rsvg-convert -w 1024 -h 1024 "$TMP/$1.svg" -o "$TMP/$1-big.png"
  # brands spec: square, trimmed, transparent
  magick "$TMP/$1-big.png" -trim +repage -background none -gravity center \
    -extent "%[fx:max(w,h)]x%[fx:max(w,h)]" -resize "$3x$3" -strip "$OUT/$1.png"
}
logo() { # name icon-png text-colour height
  sed -e "s#ICON#$TMP/$2-big.png#" -e "s/TEXT/$3/" logo.svg > "$TMP/$1.svg"
  rsvg-convert -h 1024 "$TMP/$1.svg" -o "$TMP/$1-big.png"
  magick "$TMP/$1-big.png" -trim +repage -resize "x$4" -strip "$OUT/$1.png"
}

icon icon        "#263238" 256;  icon icon@2x      "#263238" 512
icon dark_icon   "#CFD8DC" 256;  icon dark_icon@2x "#CFD8DC" 512
logo logo        icon      "#263238" 128;  logo logo@2x      icon      "#263238" 256
logo dark_logo   dark_icon "#ECEFF1" 128;  logo dark_logo@2x dark_icon "#ECEFF1" 256
magick identify -format "%f %wx%h\n" "$OUT"/*.png

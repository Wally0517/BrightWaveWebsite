const sharp = require('sharp');
const path = require('path');
const fs = require('fs');

const SRC_LOGO = path.resolve(__dirname, '../../BrightWaveWebsite-main/assets/images/brightwave-logo.png');
const ASSETS_DIR = path.resolve(__dirname, '../assets');
const BG = { r: 11, g: 18, b: 32, alpha: 1 };

if (!fs.existsSync(ASSETS_DIR)) fs.mkdirSync(ASSETS_DIR, { recursive: true });

async function buildSquare(outFile, size, logoFraction) {
  const logoSize = Math.round(size * logoFraction);
  const resizedLogo = await sharp(SRC_LOGO)
    .resize(logoSize, logoSize, { fit: 'contain', background: { r: 0, g: 0, b: 0, alpha: 0 } })
    .png()
    .toBuffer();

  await sharp({
    create: { width: size, height: size, channels: 4, background: BG }
  })
    .composite([{ input: resizedLogo, gravity: 'center' }])
    .png()
    .toFile(outFile);

  console.log('Wrote', path.relative(process.cwd(), outFile), size + 'x' + size);
}

(async () => {
  await buildSquare(path.join(ASSETS_DIR, 'icon-only.png'), 1024, 0.72);
  await buildSquare(path.join(ASSETS_DIR, 'icon-foreground.png'), 1024, 0.55);
  await sharp({
    create: { width: 1024, height: 1024, channels: 4, background: BG }
  }).png().toFile(path.join(ASSETS_DIR, 'icon-background.png'));
  console.log('Wrote assets/icon-background.png 1024x1024');

  await buildSquare(path.join(ASSETS_DIR, 'splash.png'), 2732, 0.30);
  await buildSquare(path.join(ASSETS_DIR, 'splash-dark.png'), 2732, 0.30);
})();

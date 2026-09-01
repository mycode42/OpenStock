const sharp = require('sharp');
const fs = require('fs');
const path = require('path');

const src = '/home/ubuntu/.openclaw/media/tool-image-generation/favicon---01170733-7740-4cd3-bb65-bc412f3b72a0.jpg';
const outDir = '/home/ubuntu/openstock/www';

const sizes = [16, 32, 48, 64, 128, 192, 256];

(async () => {
  // Create PNGs for each size
  for (const size of sizes) {
    await sharp(src)
      .resize(size, size)
      .png()
      .toFile(path.join(outDir, 'favicon-' + size + '.png'));
    console.log('Created favicon-' + size + '.png');
  }

  // Create multi-size favicon.ico
  const icoBuffer = await sharp(src)
    .resize(32, 32)
    .png()
    .toBuffer();
  await sharp(icoBuffer)
    .toFile(path.join(outDir, 'favicon.ico'));
  console.log('Created favicon.ico');

  // Create apple-touch-icon
  await sharp(src)
    .resize(180, 180)
    .png()
    .toFile(path.join(outDir, 'apple-touch-icon.png'));
  console.log('Created apple-touch-icon.png');

  // Create web manifest
  const manifest = {
    name: 'OpenStock',
    short_name: 'OpenStock',
    start_url: '/',
    display: 'standalone',
    background_color: '#000000',
    theme_color: '#C8FF00',
    icons: [
      { src: '/favicon-16.png', sizes: '16x16', type: 'image/png' },
      { src: '/favicon-32.png', sizes: '32x32', type: 'image/png' },
      { src: '/favicon-48.png', sizes: '48x48', type: 'image/png' },
      { src: '/favicon-64.png', sizes: '64x64', type: 'image/png' },
      { src: '/favicon-128.png', sizes: '128x128', type: 'image/png' },
      { src: '/favicon-192.png', sizes: '192x192', type: 'image/png' },
      { src: '/favicon-256.png', sizes: '256x256', type: 'image/png' },
      { src: '/apple-touch-icon.png', sizes: '180x180', type: 'image/png' }
    ]
  };
  fs.writeFileSync(path.join(outDir, 'site.webmanifest'), JSON.stringify(manifest, null, 2));
  console.log('Created site.webmanifest');

  console.log('Done!');
})();

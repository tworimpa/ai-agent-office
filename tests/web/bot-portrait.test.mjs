import test from 'node:test';
import assert from 'node:assert/strict';
import { canvasPortrait } from '../../src/agent_office/web/bot-client-ui.js';

// Edge 158, 3 × 2 sRGB canvas, two opaque columns and one transparent, WebP quality 0.8.
// Keep the encoder's actual ICC profile, padded odd-sized ALPH and VP8 bitstream.
const edgeCanvas = Buffer.from(
  'UklGRiQCAABXRUJQVlA4WAoAAAAwAAAAAgAAAQAASUNDUMgBAAAAAAHIAAAAAAQwAABtbnRyUkdCIFhZWiAH4AABAAEAAAAAAABhY3NwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAA9tYAAQAAAADTLQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAlkZXNjAAAA8AAAACRyWFlaAAABFAAAABRnWFlaAAABKAAAABRiWFlaAAABPAAAABR3dHB0AAABUAAAABRyVFJDAAABZAAAAChnVFJDAAABZAAAAChiVFJDAAABZAAAAChjcHJ0AAABjAAAADxtbHVjAAAAAAAAAAEAAAAMZW5VUwAAAAgAAAAcAHMAUgBHAEJYWVogAAAAAAAAb6IAADj1AAADkFhZWiAAAAAAAABimQAAt4UAABjaWFlaIAAAAAAAACSgAAAPhAAAts9YWVogAAAAAAAA9tYAAQAAAADTLXBhcmEAAAAAAAQAAAACZmYAAPKnAAANWQAAE9AAAApbAAAAAAAAAABtbHVjAAAAAAAAAAEAAAAMZW5VUwAAACAAAAAcAEcAbwBvAGcAbABlACAASQBuAGMALgAgADIAMAAxADZBTFBIBwAAAAAA//8A//8AVlA4ICYAAACwAQCdASoDAAIAAUAmJZACdLoABKoAAP6uF/i2kdCp1epGGLmQAA==',
  'base64',
);
function packedChunks(webp) {
  assert.equal(webp.length - 8, webp.readUInt32LE(4));
  const chunks = [];
  for (let p = 12; p < webp.length; ) {
    const length = webp.readUInt32LE(p + 4);
    const end = p + 8 + length + (length % 2);
    chunks.push(webp.subarray(p, end));
    p = end;
  }
  return chunks;
}
function wrap(chunks) {
  const result = Buffer.concat([Buffer.from('RIFF    WEBP'), ...chunks]);
  result.writeUInt32LE(result.length - 8, 4);
  return result;
}
function portable(bytes, w = 3, h = 2) {
  const uri = canvasPortrait(globalThis, 'data:image/webp;base64,' + bytes.toString('base64'), w, h);
  return Buffer.from(uri.split(',')[1], 'base64');
}

test('real Edge sRGB WebP drops ICCP while preserving the original alpha padding and image bytes', () => {
  const source = packedChunks(edgeCanvas);
  assert.deepEqual(
    source.map((chunk) => chunk.toString('ascii', 0, 4)),
    ['VP8X', 'ICCP', 'ALPH', 'VP8 '],
  );
  assert.equal(source[1].readUInt32LE(4), 456);
  const image = portable(edgeCanvas),
    chunks = packedChunks(image);
  assert.equal(source[0][8], 0x30);
  assert.equal(chunks[0][8], 0x10);
  assert.deepEqual(chunks.slice(1), source.slice(2));
  assert.deepEqual(portable(image), image);
});
test('opaque canvas output has a simple RIFF; malformed dimensions, chunks and lengths fail locally', () => {
  const [header, profile, , pixels] = packedChunks(edgeCanvas);
  const opaque = Buffer.from(header);
  opaque[8] = 0x20;
  const basic = wrap([pixels]);
  assert.deepEqual(portable(wrap([opaque, profile, pixels])), basic);
  assert.deepEqual(portable(basic), basic);
  assert.throws(() => portable(basic, 2, 3));
  assert.throws(() => portable(edgeCanvas.subarray(0, -1)));
  assert.throws(() => portable(wrap([header, pixels])));
  assert.throws(() => portable(wrap([profile, pixels])));
});

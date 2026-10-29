'use strict';
import('node:path').then((p) => {
  console.log(JSON.stringify({ mode: globalThis.__preloadMode, imported: typeof p.join === 'function' }));
});

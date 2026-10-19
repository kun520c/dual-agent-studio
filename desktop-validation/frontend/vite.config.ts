import { defineConfig } from '../../design-prototype/node_modules/vite';
import { resolve } from 'node:path';
export default defineConfig({
  root: resolve(import.meta.dirname),
  base: '/',
  build: { outDir:'dist', emptyOutDir:true },
  resolve: { alias: {
    'react-dom':resolve(import.meta.dirname,'../../design-prototype/node_modules/react-dom'),
    'react':resolve(import.meta.dirname,'../../design-prototype/node_modules/react'),
    'lucide-react':resolve(import.meta.dirname,'../../design-prototype/node_modules/lucide-react'),
  } },
});

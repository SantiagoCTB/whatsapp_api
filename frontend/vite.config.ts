import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/get_chat_list': 'http://localhost:5000',
      '/get_chat': 'http://localhost:5000',
      '/send_message': 'http://localhost:5000',
      '/get_botones': 'http://localhost:5000',
      '/send_image': 'http://localhost:5000',
      '/send_audio': 'http://localhost:5000',
      '/send_video': 'http://localhost:5000',
      '/set_alias': 'http://localhost:5000'
    }
  },
  build: {
    outDir: '../static',
    rollupOptions: {
      input: resolve(__dirname, 'index.html')
    }
  }
});

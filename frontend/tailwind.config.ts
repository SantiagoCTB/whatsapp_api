import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        primary: 'var(--color-primary)',
        secondary: 'var(--color-secondary)',
      },
      backgroundImage: {
        'gradient-primary': 'linear-gradient(to bottom right, var(--color-primary), var(--color-secondary))',
      },
      boxShadow: {
        elegant: 'var(--shadow-elegant)',
      },
    },
  },
  plugins: [],
} satisfies Config;

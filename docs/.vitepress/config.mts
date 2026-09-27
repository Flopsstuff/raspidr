import { defineConfig } from 'vitepress'

const repo = 'https://github.com/Flopsstuff/raspidr'

export default defineConfig({
  title: 'RaspiDR',
  description: 'Voice assistant on a Raspberry Pi Zero 2 W: custom wake word, Groq STT/TTS, Hermes agent',
  lang: 'en-US',
  base: '/raspidr/',
  cleanUrls: true,
  lastUpdated: true,

  themeConfig: {
    nav: [
      { text: 'Demo', link: '/demo' },
      { text: 'Architecture', link: '/architecture' },
      { text: 'Hardware', link: '/hardware' },
      { text: 'Wake word training', link: '/wakeword_training' },
    ],
    sidebar: [
      {
        text: 'Documentation',
        items: [
          { text: 'Overview', link: '/' },
          { text: 'Demo', link: '/demo' },
          { text: 'Architecture', link: '/architecture' },
          { text: 'Hardware', link: '/hardware' },
          { text: 'Wake word training', link: '/wakeword_training' },
        ],
      },
    ],
    outline: [2, 3],
    search: { provider: 'local' },
    socialLinks: [{ icon: 'github', link: repo }],
    editLink: { pattern: `${repo}/edit/main/docs/:path`, text: 'Edit this page on GitHub' },
    footer: {
      message: 'Code and docs: MIT. Wake word model: non-commercial (CC BY-NC-SA 4.0).',
    },
  },
})

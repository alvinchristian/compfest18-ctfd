import { resolve } from "path";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import copy from "rollup-plugin-copy";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "~": resolve(__dirname, "./node_modules/"),
    },
  },
  build: {
    manifest: true,
    outDir: "static",
    rollupOptions: {
      plugins: [
        copy({
          targets: [
            {
              src: "./node_modules/@fortawesome/fontawesome-free/webfonts/**/*",
              dest: "static/webfonts",
            },
            {
              src: "./assets/fonts/compfest18/Compfest18-SemiBold.otf",
              dest: "static/webfonts/compfest18",
            },
            {
              src: "./assets/fonts/montserrat/Montserrat-SemiBold.otf",
              dest: "static/webfonts/montserrat",
            },
            {
              src: "./assets/fonts/plus-jakarta-sans/PlusJakartaSans-Regular.otf",
              dest: "static/webfonts/plus-jakarta-sans",
            },
            {
                src: "./assets/img/**",
                dest: "static/img",
            },
            {
            src: "./assets/sounds/**",
            dest: "static/sounds",
            },
          ],
          hook: "writeBundle",
        }),
      ],
      output: {
        manualChunks: {
          echarts: ["echarts", "zrender"],
        },
      },
      input: {
        main: resolve(__dirname, "assets/scss/main.scss"),
        color_mode_switcher: resolve(__dirname, "assets/js/color_mode_switcher.js"),
        sidebar_state: resolve(__dirname, "assets/js/sidebar_state.js"),
        page: resolve(__dirname, "assets/js/page.js"),
        setup: resolve(__dirname, "assets/js/setup.js"),
        settings: resolve(__dirname, "assets/js/settings.js"),
        challenges: resolve(__dirname, "assets/js/challenges.js"),
        scoreboard: resolve(__dirname, "assets/js/scoreboard.js"),
        notifications: resolve(__dirname, "assets/js/notifications.js"),

        teams_public: resolve(__dirname, "assets/js/teams/public.js"),
        teams_private: resolve(__dirname, "assets/js/teams/private.js"),
        teams_list: resolve(__dirname, "assets/js/teams/list.js"),

        users_public: resolve(__dirname, "assets/js/users/public.js"),
        users_private: resolve(__dirname, "assets/js/users/private.js"),
        users_list: resolve(__dirname, "assets/js/users/list.js"),
      },
    },
  },
});
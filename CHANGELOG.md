# CHANGELOG

<!-- version list -->

## v0.5.2 (2026-11-05)

### Bug Fixes

- Accept citations of raw paths with spaces
  ([#12](https://github.com/leonardo-marziali/llm-wiki/pull/12),
  [`ce96116`](https://github.com/leonardo-marziali/llm-wiki/commit/ce961168da30906f95578f320814e93a192db2ac))

### Chores

- Make pyright check the worktree's own source
  ([#12](https://github.com/leonardo-marziali/llm-wiki/pull/12),
  [`ce96116`](https://github.com/leonardo-marziali/llm-wiki/commit/ce961168da30906f95578f320814e93a192db2ac))


## v0.5.1 (2026-11-05)

### Bug Fixes

- Test every supported Python version before publishing
  ([#11](https://github.com/leonardo-marziali/llm-wiki/pull/11),
  [`69b95fb`](https://github.com/leonardo-marziali/llm-wiki/commit/69b95fb17724089598e90fb59500226d2aaf12dc))

### Chores

- Add Python version classifiers ([#10](https://github.com/leonardo-marziali/llm-wiki/pull/10),
  [`074a5f8`](https://github.com/leonardo-marziali/llm-wiki/commit/074a5f8f642773842a7ec25a0849eb91f3eeb34e))


## v0.5.0 (2026-11-05)

### Bug Fixes

- Keep embeds' names when a plain link shares their file name
  ([#9](https://github.com/leonardo-marziali/llm-wiki/pull/9),
  [`073317b`](https://github.com/leonardo-marziali/llm-wiki/commit/073317be54fbd0028f29f84ffac28575405f3d36))

### Documentation

- Explain linked images in the manual and close M8
  ([#9](https://github.com/leonardo-marziali/llm-wiki/pull/9),
  [`073317b`](https://github.com/leonardo-marziali/llm-wiki/commit/073317be54fbd0028f29f84ffac28575405f3d36))

- Explain what upgrading does to plain image links
  ([#9](https://github.com/leonardo-marziali/llm-wiki/pull/9),
  [`073317b`](https://github.com/leonardo-marziali/llm-wiki/commit/073317be54fbd0028f29f84ffac28575405f3d36))

### Features

- Count images a page only links as part of that page
  ([#9](https://github.com/leonardo-marziali/llm-wiki/pull/9),
  [`073317b`](https://github.com/leonardo-marziali/llm-wiki/commit/073317be54fbd0028f29f84ffac28575405f3d36))


## v0.4.0 (2026-11-04)

### Features

- Ask for a git identity in wiki init ([#8](https://github.com/leonardo-marziali/llm-wiki/pull/8),
  [`1c367be`](https://github.com/leonardo-marziali/llm-wiki/commit/1c367bebdce07649b5a5b56b6f8154a1136aa97e))


## v0.3.0 (2026-11-04)

### Bug Fixes

- Keep bundle images safe through failed runs and odd pages
  ([#7](https://github.com/leonardo-marziali/llm-wiki/pull/7),
  [`4c360ef`](https://github.com/leonardo-marziali/llm-wiki/commit/4c360ef3faedf0a6f8411e244dff36f4d413d305))

- Reject an image locator with a mangled name
  ([#7](https://github.com/leonardo-marziali/llm-wiki/pull/7),
  [`4c360ef`](https://github.com/leonardo-marziali/llm-wiki/commit/4c360ef3faedf0a6f8411e244dff36f4d413d305))

### Documentation

- Close M7 with the real-run result ([#7](https://github.com/leonardo-marziali/llm-wiki/pull/7),
  [`4c360ef`](https://github.com/leonardo-marziali/llm-wiki/commit/4c360ef3faedf0a6f8411e244dff36f4d413d305))

- Specify page bundles and plan M7 ([#7](https://github.com/leonardo-marziali/llm-wiki/pull/7),
  [`4c360ef`](https://github.com/leonardo-marziali/llm-wiki/commit/4c360ef3faedf0a6f8411e244dff36f4d413d305))

### Features

- Cite images by name in a page's plan and edit
  ([#7](https://github.com/leonardo-marziali/llm-wiki/pull/7),
  [`4c360ef`](https://github.com/leonardo-marziali/llm-wiki/commit/4c360ef3faedf0a6f8411e244dff36f4d413d305))

- Find the local images a Markdown page links
  ([#7](https://github.com/leonardo-marziali/llm-wiki/pull/7),
  [`4c360ef`](https://github.com/leonardo-marziali/llm-wiki/commit/4c360ef3faedf0a6f8411e244dff36f4d413d305))

- Ingest a page and its images as one source
  ([#7](https://github.com/leonardo-marziali/llm-wiki/pull/7),
  [`4c360ef`](https://github.com/leonardo-marziali/llm-wiki/commit/4c360ef3faedf0a6f8411e244dff36f4d413d305))

- Record a page's images in its manifest entry
  ([#7](https://github.com/leonardo-marziali/llm-wiki/pull/7),
  [`4c360ef`](https://github.com/leonardo-marziali/llm-wiki/commit/4c360ef3faedf0a6f8411e244dff36f4d413d305))


## v0.2.0 (2026-11-03)

### Chores

- Add SonarQube project settings ([#5](https://github.com/leonardo-marziali/llm-wiki/pull/5),
  [`d30cd0c`](https://github.com/leonardo-marziali/llm-wiki/commit/d30cd0ce9db1df2d554bd1d372fdc32f39573a5f))

### Continuous Integration

- Install only wheels and pin build tools in the publish build
  ([#4](https://github.com/leonardo-marziali/llm-wiki/pull/4),
  [`9ce3e17`](https://github.com/leonardo-marziali/llm-wiki/commit/9ce3e174398fcdfbcbeb07251a05f5b789408a07))

### Documentation

- Add PyPI version badges to the README ([#3](https://github.com/leonardo-marziali/llm-wiki/pull/3),
  [`9340bd0`](https://github.com/leonardo-marziali/llm-wiki/commit/9340bd0bfd6ba6708e2ab9d5268b6a91ac5d8b2b))

### Features

- Search several chosen wikis at once ([#6](https://github.com/leonardo-marziali/llm-wiki/pull/6),
  [`8a170ad`](https://github.com/leonardo-marziali/llm-wiki/commit/8a170ad381286435efe8c3a452270abfd5f366dc))


## v0.1.1 (2026-10-30)

### Bug Fixes

- Ignore OS metadata files in new wikis ([#2](https://github.com/leonardo-marziali/llm-wiki/pull/2),
  [`a031722`](https://github.com/leonardo-marziali/llm-wiki/commit/a0317223fb2eb5c2275b40a76605b7cf5f86fc8e))


## v0.1.0 (2026-10-30)

- Initial Release

# Third-party components

The MIT license in this repository applies to OpenEmbeddLint's own code.
Third-party software retains its original license.

- The bundled Lizard 1.25.0 Python distribution is MIT licensed. Its copyright
  and license are preserved in `tools/python/lizard-1.25.0/lizard-1.25.0.dist-info/licenses/LICENSE.txt`.
- Lizard's Pygments and pathspec runtime dependencies are declared in
  `pyproject.toml` and installed by the Python package manager from PyPI;
  their upstream licenses remain applicable.
- LLVM/Clang, Cppcheck, CMake, Ninja and their native dependencies are external
  executables, not part of the Python wheel or source distribution. Download
  them from the upstream links in the README. Their package URLs, versions,
  hashes and license metadata are recorded in `tools/manifest.json` and
  `tools/THIRD_PARTY_NOTICES.json`. Local installations may retain upstream
  license texts and recipes under the ignored `tools/notices/` directory.
  Those caches are not part of the public source or Python distributions;
  a recipe is not a claim that all corresponding source has been redistributed.
- OpenEmbeddLint does not redistribute MISRA guideline text. The Cppcheck
  addon can diagnose rule IDs without that text. A separately authorized local
  rule-text file is optional and stays outside this repository and its releases.
- Apple SDKs and Microsoft SDKs are not redistributed by this project. macOS
  host compilation requires the user's Xcode Command Line Tools SDK.

Publishing a separately assembled native tool bundle requires its own license
and corresponding-source review. The public release assets contain the Python
CLI and source only.

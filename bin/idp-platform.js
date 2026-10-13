#!/usr/bin/env node
'use strict';
require('../cli').run(process.argv.slice(2)).catch(error => {
  console.error(`Error: ${error.message}\nFIX: inspect idp-platform --help and retry.`);
  process.exitCode = require('node:os').constants.errno.EINVAL;
});

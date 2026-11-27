# Privacy policy of the cabildo Chrome extension

The cabildo extension sends nothing to its developer or to any server of cabildo. It passes data
of the tabs you share to the cabildo program on your computer.

Last change: 2026-11-28.

## What the extension reads

Only tabs in a tab group named `cabildo`, and only while an agent is connected:

- the address and title of the tab;
- the content of the page, screenshots of it, and what the page writes to the console;
- the requests the page makes and the replies, without cookie headers;
- what the agent types, clicks or uploads there.

Pages you share can hold personal data. Share only tabs whose content your agent may read.

## Where the data goes

The extension gives it to the `cabildo` program on the same computer, over Chrome's native
messaging. cabildo gives it to the agent you started, such as Claude Code. That agent can send it
to the AI model provider you configured, under that provider's privacy policy.

## What the extension stores

One setting in Chrome: whether the switch in the popup is on. cabildo can save files an agent
asks for, such as screenshots, on your computer.

## What the extension does not do

- It does not sell or transfer your data to anyone else.
- It does not use your data for advertising, profiling or credit decisions.
- Its developer cannot see your data.

The use of information received from Chrome's APIs follows the
[Chrome Web Store User Data Policy](https://developer.chrome.com/docs/webstore/program-policies/user-data-faq),
including its Limited Use requirements.

## Stop it

Turn the switch in the popup off, remove a tab from the `cabildo` group, or remove the extension.

## Contact

[Open an issue](https://gitlab.com/cardona-oss/cabildo/-/issues).

# Bugs
- [x] Deleting images in the fetch card art workflow using the clear card images button clears the images but changes the command preview to say "waiting for server". Toggling an option on and off brings the command preview back.
- [x] The extra settings under the decklist format dropdown used to be in a collapsable region defaulting to collapsed but are now part of the main UI. Move them back to the collapsed section.
- [x] The decklist source and picker are in 2 columns but it also didn't used to be like this. The decklist picker should be under the decklist source.
- [x] Create PDF page is blank in simple mode using the 212071c macos artifact
- [x] If github doens't provide content length for downloads then make sure the progress bar uses the cycling animation
- [x] It's possible to click the download latest button while there's already a download in progress, it doesn't seem to do anything but it does show a toast that a job has been started. Only let the button be clicked if there isn't a job already running.
- [x] Make the release job in the package workflow delete the action artifacts after it's finished uploading and attaching to the release
- [x] Remove this text from the command preview "⚠ Extras size “standard_mtg” detected — SCM_EXTRA_LAYOUTS is set automatically." in the create pdf workflow. There's also a toast when the job starts that can be removed.
- [x] In the fetch card art page with Magic selected, the MTG card preferences expansion settings have all the toggles in individual rows. Use the standard 4 column layout.
- [x] In advanced mode when you open the console it overlaps the pages. Make sure the console doesn't overlap pages.
- [x] If you start the create PDF job and change pages and the job finishes, you don't get the open pdf button.

# Features
- [x] The macos build workflow double zips the app. main zip -> inside zip -> SCM Workbench.app. Instead of fixing the zip situation let's implement the proper solution by making the file the macos standard installer method where a UI box opens and prompts you to drag the app to the applications folder and you physically drag the app icon over the applications icon (a `.dmg` file). Windows stays as the "portable" solution.
- [x] Create a page that's only used on a fresh boot to show the repos being downloaded. The page should be able to be closed out so the user can navigate around if they want but this should be a nice way to ease people in.
- [x] When the create PDF job is running, make the progress bar a real progress bar. The script outputs which image has been added by number ("Image 1: xyz.png", "Image 2: abc.png") and we can get the total from the number of images in the front image directory.
- [x] When the create PDF job has finished, along side the open PDF button that appears add a button to open the cutting template based on the PDF settings were used.

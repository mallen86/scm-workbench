/* Help for the pages that are currently in Workbench. Keep control names in
   sync with their pages; Simple mode must not describe hidden controls. */

export const PAGE_HELP = {
  fetch: {
    title: "Fetch card art",
    intro: "Download the card images for your deck before making a PDF.",
    sections: [
      { title: "Choose a game and decklist", text: "Choose your game, then use the decklist controls below it. You can choose a saved decklist or paste text. Some games also accept a URL. Choose the format that matches your decklist; the available choices depend on the game." },
      { title: "Fetch the images", text: "Check the options, then start the fetch. Watch the progress and any warnings about missing images. If images are missing, check the warnings and your decklist. Before starting a full fetch again, use Clear card images. Fetching again does not fill only the gaps; it can duplicate images that were already fetched." },
      { title: "What comes next?", text: "Go to Create PDF when the images are ready. Image post-processing is optional: use it first if you want to enlarge the images with an upscaler." },
      { title: "Starting a different deck", text: "Fetching does not clear old images. Use Clear card images before fetching a different deck, or old cards can end up in your new PDF. This permanently deletes the front and double-sided images. Your shared card back stays." },
    ],
  },
  postprocess: {
    title: "Image post-processing",
    intro: "This page is optional. You can make a PDF without running an upscaler.",
    sections: [
      { title: "Simple Upscaler", text: "Ready to use, with no download. It makes each image four times wider and four times taller, using a standard image-resizing method. It does not use AI." },
      { title: "Advanced AI Upscaler", text: "Uses an AI model to enlarge images by the same amount. Choose Install model & libraries before using it. The download includes a 67 MB model and its required libraries; leave at least 2 GB free during installation. It tries to use a graphics processor (GPU) when the hardware, drivers, and libraries support one; otherwise it uses the main processor (CPU). CPU-only runs can take considerably longer, especially for a large deck." },
      { title: "Choose which images to change", text: "Front and double-sided includes those two groups, not the shared card back. Front only and Double-sided only limit the run to one group. Back only processes the shared card-back images. Choose a processor and scope, then use Run processor." },
      { title: "Use original images", text: "A successful run replaces the selected images. Running an upscaler again enlarges them again, so restore or re-fetch the originals before trying a different upscaler. If a run fails or you cancel it, the original image batch stays unchanged." },
      { title: "Remove the AI download", text: "Remove model & libraries frees the space used by the Advanced Upscaler. It does not undo changes to your images. You can reinstall it later, and the Simple Upscaler stays available." },
      { title: "Custom processors", mode: "advanced", text: "The library lets you create, import, or duplicate Python processors. Saving makes a new revision that you must trust before running. Only use code and libraries you trust: they run with your access to your computer. Duplicating the Advanced Upscaler copies its source, not its installed model or libraries. The Guide button explains how custom processors work." },
    ],
  },
  pdf: {
    title: "Create PDF",
    intro: "Turn your card images into sheets you can print and cut. Fetch the images first. Upscaling them is optional.",
    sections: [
      { title: "Choose card and paper sizes", text: "Card size sets the finished card dimensions. Paper size should match the sheets in your printer. These choices determine how many cards fit on a page and which cutting template to use. Borderless uses a tighter layout, so use a matching borderless template." },
      { title: "Add a shared card back", text: "Use the card-back control to choose a back image, replace it, or remove it. This is the shared back for your cards, separate from cards that have their own front and back art." },
      { title: "Print and image options", mode: "simple", text: "Apply saved offset uses the printer correction saved on Offset & calibration. Skip bottom-left position leaves that card position empty, which can help with registration issues. MPCFill automatically adds 3 mm of bleed (extra image around the edges). MPCFill Crop removes that extra bleed from front images. Extend Corners extends rounded image corners by 3.5 mm. You can leave it on even if your images do not need it." },
      { title: "Folders, quality, and edge finishing", mode: "advanced", text: "The source folders choose which images to use; Output PDF chooses where the result is saved. Higher PPI means more pixels per printed inch and can take longer to process. Stretch fills a card but can distort the art. Center crop keeps its proportions but cuts off edges. Crop and extension values change the image edges, not the finished card size." },
      { title: "Check the preview", text: "First Page Preview shows a low-quality estimate of the first front page, not the whole deck. Click it to enlarge it. Check the layout and any warnings before starting the PDF." },
      { title: "Create and open the result", text: "Start the job when you are happy with the settings. When it finishes, use Open PDF to inspect the file. You can also open the matching cutting template when one is available." },
    ],
  },
  offset: {
    title: "Offset & calibration",
    intro: "Use this page if the fronts and backs of your printed cards do not line up. You can skip it if your prints already align.",
    sections: [
      { title: "Measure a test print", text: "Open the calibration sheet for your paper size. Print both pages on opposite sides of one sheet using a long-edge flip, then compare the front and back dot grids." },
      { title: "Regenerate calibration sheets", text: "Regenerate all rebuilds the calibration PDFs for the available paper sizes. Use it if a sheet is missing or needs to be refreshed; you do not need to run it each time you measure an offset." },
      { title: "Enter the correction", text: "X shifts the back page sideways: a positive value moves it right. Y shifts the back page vertically: a positive value moves it up. Directions are relative to the back page as you face it. Each X or Y unit is 1/300 inch, regardless of the PDF's PPI. Angle rotates the back page in degrees; positive is clockwise. Zero clears the fields; press Save if you want to store those zero values." },
      { title: "Save by paper size", text: "Different paper sizes may need different corrections. Save for this size stores a row for the selected paper. Load puts a saved row back into the fields for editing; delete removes that row. Saving a row also updates the global offset." },
      { title: "Use the saved correction", text: "On Create PDF, turn on Apply saved offset. Workbench uses the row for that paper size, or the global offset if no row matches. Saving a correction here does not change PDFs you have already made." },
      { title: "Correct an existing PDF", mode: "advanced", text: "Offset PDF applies a correction to an existing PDF. Choose the input file and review the values before starting. If you use the saved values, check that the selected paper size is the one you printed on." },
    ],
  },
  history: {
    title: "Job history",
    intro: "A job is a task you started, such as fetching card art, making a PDF, or installing an upscaler.",
    sections: [
      { title: "See what happened", text: "Running now shows work still in progress. Finished lists completed jobs, newest first. Each entry shows its status and when it started. A failed job did not finish successfully." },
      { title: "Reuse a job's settings", text: "Click a job that has a page to open that page with the settings it used. This does not run it again. Review the settings and available files before starting another job." },
      { title: "Pages in Simple mode", mode: "simple", text: "Some jobs belong to pages that are available only in Advanced mode. If Workbench asks you to switch modes, use Advanced in the sidebar and open the job again." },
      { title: "Read the output", mode: "advanced", text: "Use Log to open the job's output in the console. This is useful for checking warnings or finding out why a job failed." },
      { title: "History is not a backup", text: "Opening an old job restores its settings, not the images or output files it used. Files that have been moved, replaced, or deleted may no longer be available." },
    ],
  },
  templates: {
    title: "Cutting templates",
    intro: "Make or open cutting templates for the card and paper sizes you use.",
    sections: [
      { title: "Match your PDF", text: "Use the same card size, paper size, and default or borderless layout as your PDF. A template for a different layout will not line up with the printed cards." },
      { title: "Make a template", text: "Use the single-template form for one combination. Choose a known size or enter custom dimensions and a name. The batch form generates multiple templates. Read any warnings before starting." },
      { title: "Open an existing template", text: "The gallery separates default and borderless files. Click a file to open it. DXF files contain the cut outlines; .studio3 files include the paper and registration settings for Silhouette Studio." },
      { title: "Delete with care", text: "The × on a DXF tile permanently deletes that file after confirmation. It does not delete card images or PDFs." },
      { title: "MTG and Sorcery sizes", text: "The Extras page has the additional card sizes and their matching cutting templates." },
    ],
  },
  extras: {
    title: "Extras: MTG & Sorcery",
    intro: "Find the extra card sizes and cutting templates for Magic: The Gathering and Sorcery: Contested Realm.",
    sections: [
      { title: "Use an extra size", text: "Click a card-size tile to open Create PDF with that size selected. Extra sizes are also included in the Create PDF size list when scm-extras is connected." },
      { title: "Read the layout table", text: "Each cell shows columns × rows for a card and paper combination. The smaller line marked bl shows the borderless layout. A dash means that combination has no listed layout." },
      { title: "Generate or open templates", text: "Use the generation form to make the extras templates. The gallery below contains the available DXF and .studio3 files. Choose the file that matches the PDF's card size, paper size, and borderless setting." },
      { title: "Print tables to console", text: "This creates a text listing of the extras layouts in the job console. It does not print anything on your printer." },
      { title: "If this page is empty", text: "Check scm-extras under Managed repo copies in Settings. The sizes and templates come from that copy." },
    ],
  },
  sizes: {
    title: "Sizes & layouts",
    intro: "Compare the available card and paper sizes before making a PDF.",
    sections: [
      { title: "Choose a card size", text: "Each card tile shows its dimensions in millimetres. Click it to open Create PDF with that size selected. Names marked extras come from scm-extras." },
      { title: "Paper and specialty layouts", text: "The paper tiles show sheet dimensions. Specialty layouts list fixed card arrangements for particular paper sizes." },
      { title: "Read Cards per page", text: "Find your card size in a row and paper size in a column. Each cell shows columns × rows and the total cards per page. A dash means no layout is listed for that combination." },
      { title: "Default or Borderless", text: "The switch changes which layout counts the table shows. It does not change your PDF settings. Choose the same layout on Create PDF and use its matching cutting template." },
    ],
  },
  utilities: {
    title: "Utilities",
    intro: "Clear old card images, convert measurements, or list the available sizes.",
    sections: [
      { title: "Start a new game", text: "Clear front & double-sided permanently deletes those card images after you confirm. Shared card backs stay. Clear old images before fetching a different deck so they do not get mixed into the new PDF." },
      { title: "Convert a measurement", text: "Enter a value, then choose the From and To units. The result updates as you type. PPI means pixels per inch and matters when converting to or from pixels; it does not change your images or PDF settings." },
      { title: "List every known size", text: "Run writes the known card and paper sizes to the job console, including extras. It does not make a PDF or a cutting template." },
    ],
  },
  settings: {
    title: "Settings",
    intro: "Set your PDF defaults, manage the tools Workbench uses, and check the app version.",
    sections: [
      { title: "Create PDF defaults", text: "Choose the card size, paper size, PPI, and quality you usually want, then Save defaults. These are starting values for Create PDF, not changes to PDFs you have already made. PPI means pixels per inch; higher values can take longer to process and produce larger files." },
      { title: "Guided tutorial", text: "Start guided tutorial walks you through fetching images, optional upscaling, and making a PDF. You can stop at any step. The tutorial does not run jobs for you." },
      { title: "Managed repo copies", text: "These are the local copies of silhouette-card-maker and scm-extras that supply the card tools, game information, and layouts. Check their status here and download or update them when needed. Watch the progress and wait for the work to finish before starting a task that needs them." },
      { title: "Choose tool versions and locations", mode: "advanced", text: "Managed repo copies lets you choose a release or branch. Repos lets you point Workbench at copies in other folders. Save repo paths applies those locations. Use these controls only if you want to change which tools Workbench runs." },
      { title: "App updates", runtime: "packaged", text: "Check for a newer Workbench version here. What's New describes the changes. Follow the update action shown for your system; some systems require installing the downloaded package yourself." },
      { title: "Beta releases", mode: "advanced", runtime: "packaged", text: "Include beta releases offers test versions as well as stable releases. That choice stays in effect if you switch back to Simple mode. Turn it off if you want to return to the stable update channel." },
      { title: "Python & server", mode: "advanced", runtime: "source", text: "The Python interpreter runs new jobs. Save python & server applies that choice. Port changes take effect the next time the server starts." },
      { title: "Running from source", runtime: "source", text: "App updates shows the source version you are running. Self updates are available in the packaged app, not this browser version." },
      { title: "Data & about", text: "Open data folder shows where Workbench keeps its settings, job history, and other saved data. Do not delete files there unless you mean to remove them." },
    ],
  },
  preparing: {
    title: "Getting ready",
    intro: "Workbench is preparing the tools and game information it needs before you can fetch images and make PDFs.",
    sections: [
      { title: "While setup runs", text: "The progress rows show what is being downloaded or checked. You can explore the app while setup continues. Tasks become available as their required tools are ready." },
      { title: "If setup stops", text: "Use Retry setup when it is offered. Only the copies that are not ready will be downloaded again. You can also check Managed repo copies in Settings." },
      { title: "When it is ready", text: "Got it. Show me around starts the guided tutorial. Skip tutorial takes you into the app without it. You can start the tutorial later from Settings." },
    ],
  },
};

export function pageHelpContent(page, { mode = "simple", packaged = false } = {}) {
  const help = PAGE_HELP[page];
  if (!help || !Object.prototype.hasOwnProperty.call(PAGE_HELP, page)) return null;
  return {
    title: help.title,
    intro: help.intro,
    sections: help.sections.filter(section =>
      (!section.mode || section.mode === mode) &&
      (!section.runtime || section.runtime === (packaged ? "packaged" : "source"))),
  };
}

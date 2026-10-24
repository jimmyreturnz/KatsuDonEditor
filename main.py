"""KatsuDon Editor. Run from the repository root: python main.py"""
if __name__ == "__main__":
    # The Rate Changer renders in worker processes. A frozen build starts
    # each one as this exe, and freeze_support is what turns it into a worker
    # instead of a second window; the import stays below it so a worker
    # started from source does not load the whole UI to run one function.
    import multiprocessing
    multiprocessing.freeze_support()
    from ui.gui import main
    main()

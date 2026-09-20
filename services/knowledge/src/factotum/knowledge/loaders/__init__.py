"""Reading documents: from the bytes of a file to blocks of text.

Every file is hostile until proven otherwise: the format is recognized from the content and not from
the name, the size, the pages and the text are limited, and the reading runs in a process of its
own (``isolation``) that can be killed.
"""
